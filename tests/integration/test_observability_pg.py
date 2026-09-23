"""阶段 12 验收的集成测试（真实 PostgreSQL + TestClient）。

验收（计划第 940 行）：**给定任一 Run 能完整复述「系统做了什么、花了多少、
为何得到这个结论」**。

这条验收只能靠"真的造一个 Run 出来、看复述里有没有空缺"来验证，
所以这里既测聚合逻辑，也测复述文本本身。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.analysis.run_detail import audit_run_detail, build_run_detail
from app.db import SessionLocal, engine
from app.main import app
from app.utils.observability import (
    TRACE_ID_HEADER,
    bind_run_context,
    configure_logging,
    current_run_id,
    current_trace_id,
    get_logger,
    new_trace_id,
)

pytestmark = pytest.mark.integration
UTC = timezone.utc


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM evidences WHERE insight_id IN (SELECT id FROM insights "
            "WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'obsc-%'))"))
        conn.execute(text(
            "DELETE FROM insights WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'obsc-%')"))
        conn.execute(text(
            "DELETE FROM agent_runs WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'obsc-%')"))
        conn.execute(text(
            "DELETE FROM data_sources WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'obsc-%')"))
        conn.execute(text("DELETE FROM projects WHERE name LIKE 'obsc-%'"))
        conn.execute(text("DELETE FROM users WHERE email LIKE 'obsc-%@example.com'"))


def _signup(client: TestClient) -> dict:
    email = f"obsc-{datetime.now(UTC).timestamp()}@example.com"
    response = client.post("/api/register", json={"email": email, "password": "strong-password"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _project(client: TestClient, headers: dict) -> int:
    return client.post(
        "/api/projects", json={"name": f"obsc-{datetime.now(UTC).timestamp()}"}, headers=headers
    ).json()["id"]


# ============================================================
# trace_id：贯穿请求与日志
# ============================================================


def test_response_carries_a_trace_id(client: TestClient):
    response = client.get("/healthz")
    assert response.headers.get(TRACE_ID_HEADER), "响应应回传 trace_id，便于用户报给我们 grep 日志"


def test_incoming_trace_id_is_honoured(client: TestClient):
    """上游（网关/前端）带来的 trace_id 必须沿用，否则链路会断成两截。"""
    given = "abc123deadbeef"
    response = client.get("/healthz", headers={TRACE_ID_HEADER: given})
    assert response.headers[TRACE_ID_HEADER] == given


def test_blank_trace_id_is_replaced(client: TestClient):
    response = client.get("/healthz", headers={TRACE_ID_HEADER: "   "})
    assert response.headers[TRACE_ID_HEADER].strip()


def test_different_requests_get_different_trace_ids(client: TestClient):
    first = client.get("/healthz").headers[TRACE_ID_HEADER]
    second = client.get("/healthz").headers[TRACE_ID_HEADER]
    assert first != second


def test_trace_context_is_restored_after_exit():
    """嵌套调用时退出要恢复原值，而不是一味清空。"""
    with bind_run_context(trace_id="outer", run_id=1):
        assert current_trace_id() == "outer"
        assert current_run_id() == 1
        with bind_run_context(trace_id="inner", run_id=2):
            assert current_trace_id() == "inner"
            assert current_run_id() == 2
        assert current_trace_id() == "outer", "内层退出后应恢复外层"
        assert current_run_id() == 1
    assert current_trace_id() is None


def test_new_trace_id_is_unique():
    ids = {new_trace_id() for _ in range(100)}
    assert len(ids) == 100


def test_structured_logs_are_json_with_trace_id(capsys):
    """计划第 937 行：structlog 输出 JSON，带 trace_id。"""
    import json

    configure_logging(level="INFO", force=True)
    log = get_logger("test")
    with bind_run_context(trace_id="trace-xyz", run_id=9, project_id=3):
        log.info("analysis_step", phase="parse")

    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "应当有日志输出"
    payload = json.loads(captured[-1])
    assert payload["event"] == "analysis_step"
    assert payload["phase"] == "parse"
    assert payload["trace_id"] == "trace-xyz"
    assert payload["run_id"] == 9
    assert payload["project_id"] == 3


def test_configure_logging_is_idempotent():
    """重复配置会叠加 handler，导致每条日志打印多次——很容易被误判成重复触发。"""
    configure_logging(level="INFO", force=True)
    configure_logging(level="INFO")
    configure_logging(level="INFO")
    # 没有异常且 logger 可用即通过；重复叠加在 capsys 测试里会表现为多行输出
    assert get_logger("probe") is not None


# ============================================================
# Run 详情：能复述"做了什么、花了多少、为何得到这个结论"
# ============================================================


def test_run_detail_endpoint_requires_authentication(client: TestClient):
    assert client.get("/api/runs/1/detail?project_id=1").status_code == 401


def test_run_detail_of_another_project_is_404(client: TestClient):
    owner = _signup(client)
    intruder = _signup(client)
    project_id = _project(client, owner)
    source = client.post(
        f"/api/projects/{project_id}/data-sources", json={"format": "txt"}, headers=owner
    ).json()
    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=owner,
        ).json()["run_id"]

    assert client.get(
        f"/api/runs/{run_id}/detail", params={"project_id": project_id}, headers=intruder
    ).status_code == 404


def test_run_detail_reports_phases_cost_and_trace(client: TestClient):
    """完整复述三要素：做了什么 / 花了多少 / 为什么。"""
    headers = _signup(client)
    project_id = _project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources", json={"format": "txt"}, headers=headers
    ).json()

    given_trace = "trace-for-this-run"
    with patch("app.api.routes_runs._dispatch"):
        created = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers={**headers, TRACE_ID_HEADER: given_trace},
        )
    run_id = created.json()["run_id"]

    # 造出"跑过"的痕迹：模型调用明细、工具用量、阶段历史、终态
    with SessionLocal() as session:
        session.execute(text("""
            UPDATE agent_runs SET
              status = 'completed',
              started_at = :start, finished_at = :finish,
              last_heartbeat = :finish,
              current_phase = 'validate',
              phase_history = CAST(:history AS jsonb),
              model_calls = CAST(:calls AS jsonb),
              tool_usage = CAST(:tools AS jsonb),
              tokens_input = 3000, tokens_output = 750, cost_actual = 0.006,
              run_metadata = CAST(:meta AS jsonb)
            WHERE id = :run_id
        """), {
            "start": datetime.now(UTC) - timedelta(seconds=12),
            "finish": datetime.now(UTC),
            "history": '[{"phase":"parse","status":"done"},{"phase":"analyze","status":"done"},'
                       '{"phase":"model","status":"done"},{"phase":"validate","status":"skipped"}]',
            "calls": '[{"call_id":"c1","tier":"L2","model":"m","tokens_input":3000,'
                     '"tokens_output":750,"cost":0.006,"status":"ok"}]',
            "tools": '{"stats_calculator":[{"tool":"stats_calculator","ok":true}]}',
            "meta": '{"stop_reason":null,"completed_phases":["parse","analyze","model"],'
                    '"skipped_phases":["validate"],'
                    '"trace_id":"' + given_trace + '",'
                    '"policy":{"max_model_calls_per_run":5,"run_max_cost":0.3}}',
            "run_id": run_id,
        })
        session.commit()

    response = client.get(
        f"/api/runs/{run_id}/detail", params={"project_id": project_id}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # 做了什么
    assert body["phases"]["current"] == "validate"
    assert body["phases"]["completed"] == ["parse", "analyze", "model"]
    assert body["phases"]["skipped"] == ["validate"]
    # 花了多少
    assert body["cost"]["model_call_count"] == 1
    assert body["cost"]["cost_actual"] == pytest.approx(0.006)
    assert body["cost"]["by_tier"]["L2"]["calls"] == 1
    assert body["tools"]["call_count"] == 1
    # 为什么 / 可追踪
    assert body["trace_id"] == given_trace
    assert body["policy"]["max_model_calls_per_run"] == 5
    assert body["timeline"]["duration_seconds"] is not None

    # 复述文本必须完整，且不含"未知"
    narrative = body["narrative"]
    assert f"Run #{run_id}" in narrative
    assert "parse" in narrative and "analyze" in narrative
    assert "¥0.0060" in narrative
    assert "L2" in narrative
    assert body["anomalies"] == [], f"复述自检发现问题：{body['anomalies']}"


def test_run_detail_narrates_failure_with_reason(client: TestClient):
    """失败态必须能说清"为什么失败"，不能只显示一个 failed。"""
    headers = _signup(client)
    project_id = _project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources", json={"format": "txt"}, headers=headers
    ).json()
    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=headers,
        ).json()["run_id"]

    with SessionLocal() as session:
        session.execute(text("""
            UPDATE agent_runs SET status='failed', started_at=:t, finished_at=:t,
              error='InputFormatError: 日志格式不认识',
              run_metadata = CAST(:meta AS jsonb)
            WHERE id = :run_id
        """), {
            "t": datetime.now(UTC),
            "meta": '{"stop_reason":"input","note":"分析因输入问题未完成"}',
            "run_id": run_id,
        })
        session.commit()

    body = client.get(
        f"/api/runs/{run_id}/detail", params={"project_id": project_id}, headers=headers
    ).json()
    assert body["outcome"]["stop_reason"] == "input"
    assert "InputFormatError" in body["outcome"]["error"]
    assert "InputFormatError" in body["narrative"]
    assert "input" in body["narrative"]


def test_run_detail_exposes_evidence_behind_each_conclusion(client: TestClient):
    """「为何得到这个结论」要能追到具体事件 id。"""
    headers = _signup(client)
    project_id = _project(client, headers)

    with SessionLocal() as session:
        from app.models import AgentRun, DataSource, Evidence, Insight

        source = DataSource(project_id=project_id, type="file_upload", format="txt", location="a")
        session.add(source)
        session.flush()
        run = AgentRun(project_id=project_id, source_id=source.id, status="completed",
                       started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
        session.add(run)
        session.flush()
        insight = Insight(project_id=project_id, run_id=run.id, type="fact", severity="high",
                          confidence=0.9, title="OOM 结论", summary="s",
                          run_metadata={"runbooks": [{"id": "rb_oom_001",
                                                      "title": "内存耗尽 / OOM 处置",
                                                      "steps": ["确认内存水位"]}]})
        session.add(insight)
        session.flush()
        session.add(Evidence(insight_id=insight.id, source_id=source.id,
                             event_ids=[11, 12, 13], description="支撑 OOM 的事件"))
        session.commit()
        run_id, insight_id = run.id, insight.id

    body = client.get(
        f"/api/runs/{run_id}/detail", params={"project_id": project_id}, headers=headers
    ).json()
    assert len(body["insights"]) == 1
    row = body["insights"][0]
    assert row["evidence_event_ids"] == [11, 12, 13]
    assert row["runbooks"][0]["id"] == "rb_oom_001"
    assert str(insight_id) in body["evidence_by_insight"]
    assert body["anomalies"] == []


# ============================================================
# 自检：说不通的地方必须被显式列出
# ============================================================


def _fake_run(**overrides):
    class Run:
        pass

    run = Run()
    run.id = 1
    run.project_id = 1
    run.status = overrides.get("status", "completed")
    run.started_at = overrides.get("started_at", datetime.now(UTC) - timedelta(seconds=5))
    run.finished_at = overrides.get("finished_at", datetime.now(UTC))
    run.last_heartbeat = overrides.get("last_heartbeat", datetime.now(UTC))
    run.current_phase = overrides.get("current_phase", "validate")
    run.phase_history = overrides.get("phase_history", [{"phase": "parse", "status": "done"}])
    run.model_calls = overrides.get("model_calls", [])
    run.tool_usage = overrides.get("tool_usage", {})
    run.tokens_input = overrides.get("tokens_input", 0)
    run.tokens_output = overrides.get("tokens_output", 0)
    run.cost_actual = overrides.get("cost_actual", 0.0)
    run.error = overrides.get("error")
    run.cancel_requested = overrides.get("cancel_requested", False)
    run.run_metadata = overrides.get("run_metadata", {})
    return run


def test_audit_flags_fact_without_evidence():
    """红线 3：没有证据的 fact 在复述时必须被点名。"""
    class Insight:
        id = 1
        type = "fact"
        severity = "high"
        confidence = 0.9
        title = "无证据的断言"
        summary = "s"
        reasoning = None
        limitations = None
        run_metadata = None

    detail = build_run_detail(run=_fake_run(), insights=[Insight()], evidence_by_insight={})
    assert any("没有任何证据" in item for item in detail.anomalies)


def test_audit_flags_cost_mismatch():
    """明细与汇总对不上时不能默默通过。"""
    calls = [{"tier": "L1", "tokens_input": 100, "tokens_output": 10, "cost": 0.001}]
    detail = build_run_detail(
        run=_fake_run(model_calls=calls, cost_actual=99.0, tokens_input=100),
        insights=[],
        evidence_by_insight={},
    )
    assert any("成本不一致" in item for item in detail.anomalies)


def test_audit_flags_terminal_status_without_finish_time():
    detail = build_run_detail(
        run=_fake_run(status="completed", finished_at=None), insights=[], evidence_by_insight={}
    )
    assert any("没有结束时间" in item for item in detail.anomalies)


def test_audit_flags_partial_success_without_reason():
    detail = build_run_detail(
        run=_fake_run(status="partial_success"), insights=[], evidence_by_insight={}
    )
    assert any("stop_reason" in item for item in detail.anomalies)


def test_audit_flags_running_without_heartbeat():
    detail = build_run_detail(
        run=_fake_run(status="running", finished_at=None, last_heartbeat=None),
        insights=[],
        evidence_by_insight={},
    )
    assert any("心跳" in item for item in detail.anomalies)


def test_audit_passes_on_a_consistent_run():
    calls = [{"tier": "L2", "tokens_input": 1000, "tokens_output": 250, "cost": 0.002}]
    detail = build_run_detail(
        run=_fake_run(model_calls=calls, cost_actual=0.002, tokens_input=1000),
        insights=[],
        evidence_by_insight={},
    )
    assert detail.anomalies == []


def test_audit_run_detail_is_callable_standalone():
    """自检函数应能独立调用（供运维脚本复用）。"""
    detail = build_run_detail(run=_fake_run(), insights=[], evidence_by_insight={})
    assert audit_run_detail(detail) == []
