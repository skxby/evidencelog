"""Golden Set 场景测试（计划第 952–965 行）。

每个场景跑**真实上传管道 + 真实分析链路**，再逐条对照 `expect.json`。
标注依据写在 `tests/datasets/<场景>/expect.json` 的 `sources` 里，便于人工复核。

**为什么这条测试不能有"模型不可用就跳过"的口子**：Golden Set 要判定的是
"关键 Insight 命中"，那属于确定性部分（analyzer 产出），不依赖模型。
用 router=None 跑，既省钱又让判定稳定可复现；模型输出质量不属 V1 的
自动化判定范围（计划第 965 行：V1 人工核对即可）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.models import Project, User
from app.repositories import ProjectRepository, UserRepository
from tests.golden import SCENARIOS, load_expectation, run_all, run_scenario
from tests.golden.expectations import ExpectationFormatError

pytestmark = pytest.mark.integration
UTC = timezone.utc


@pytest.fixture()
def golden_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM evidences WHERE insight_id IN (SELECT id FROM insights "
                "WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'gs-%'))"))
            for table in ("insights", "events", "agent_runs", "data_sources"):
                conn.execute(text(
                    f"DELETE FROM {table} WHERE project_id IN "
                    "(SELECT id FROM projects WHERE name LIKE 'gs-%')"))
            conn.execute(text("DELETE FROM projects WHERE name LIKE 'gs-%'"))
            conn.execute(text("DELETE FROM users WHERE email LIKE 'gs-%@example.com'"))


@pytest.fixture()
def golden_projects(golden_session, work_tmp):
    """给每个场景一个独立项目：混在一起会互相污染。"""
    user = UserRepository(golden_session).add(
        User(email=f"gs-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    golden_session.flush()
    projects = ProjectRepository(golden_session)
    ids = {}
    for scenario in SCENARIOS:
        project = projects.add(
            Project(user_id=user.id, name=f"gs-{scenario}", budget_total=10, budget_used=0)
        )
        golden_session.flush()
        ids[scenario] = project.id
    return ids, work_tmp / "uploads"


# ============================================================
# 期望文件本身必须可解析
# ============================================================


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_has_a_readable_expectation(scenario: str):
    """缺 expect.json 或缺字段一律抛错 —— "被跳过的场景"和"通过的场景"在报告里几乎一样。"""
    expectation = load_expectation(scenario)
    assert expectation.scenario == scenario
    assert expectation.description
    # 每条期望都要有依据，便于用户复核我有没有曲解计划
    assert expectation.sources, f"{scenario} 的期望没有注明依据"


def test_unknown_expectation_field_is_rejected(work_tmp):
    """拼错字段名会静默失效，故直接报错。"""
    import json

    from tests.golden.expectations import scenario_dir

    target = scenario_dir("normal", datasets_dir=work_tmp)
    target.mkdir(parents=True, exist_ok=True)
    (target / "expect.json").write_text(
        json.dumps({"tierr": "L0"}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ExpectationFormatError, match="未知字段"):
        load_expectation("normal", datasets_dir=work_tmp)


def test_missing_expectation_file_is_rejected(work_tmp):
    with pytest.raises(ExpectationFormatError, match="缺少 expect.json"):
        load_expectation("crash", datasets_dir=work_tmp)


def test_invalid_tier_is_rejected(work_tmp):
    import json

    from tests.golden.expectations import scenario_dir

    target = scenario_dir("normal", datasets_dir=work_tmp)
    target.mkdir(parents=True, exist_ok=True)
    (target / "expect.json").write_text(
        json.dumps({"tier": "L9"}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ExpectationFormatError, match="tier"):
        load_expectation("normal", datasets_dir=work_tmp)


# ============================================================
# 五个场景整体跑
# ============================================================


def test_all_five_scenarios_meet_expectations(golden_session, golden_projects):
    """验收：Golden Set 五个场景全部符合预期。"""
    project_ids, uploads_dir = golden_projects
    report = run_all(session=golden_session, project_ids=project_ids, uploads_dir=uploads_dir)

    failures = report.failures()
    assert not failures, (
        "Golden Set 未通过：\n"
        + "\n".join(f"  ✗ {c.scenario} | {c.assertion} | {c.detail}" for c in failures)
        + "\n\n" + report.render_table()
    )
    assert report.passed
    # 5 个场景都要有结果，不能有"没跑"的
    assert {o.scenario for o in report.outcomes} == set(SCENARIOS)


def test_golden_run_reports_cost(golden_session, golden_projects):
    """计划第 964 行：记录每次 Golden Run 的成本。"""
    project_ids, uploads_dir = golden_projects
    report = run_all(session=golden_session, project_ids=project_ids, uploads_dir=uploads_dir)
    payload = report.as_dict()
    assert "total_cost" in payload
    # 离线跑（router=None）不应产生模型成本
    assert report.total_cost == 0.0


def test_normal_scenario_stays_free_and_needs_no_model(golden_session, golden_projects):
    """正常日志走 L0：一次模型都不调，成本为 0。"""
    project_ids, uploads_dir = golden_projects
    outcome, result = run_scenario(
        "normal",
        session=golden_session,
        project_id=project_ids["normal"],
        uploads_dir=uploads_dir,
    )
    assert outcome.tier == "L0"
    assert result is not None and result.used_rules_only
    assert outcome.cost == 0.0
    assert outcome.insight_count == 0


@pytest.mark.parametrize(
    ("scenario", "expected_analyzer"),
    [
        ("cpu_anomaly", "CPUSpike"),
        ("memory_growth", "MemoryGrowth"),
        ("crash", "ProcessCrash"),
    ],
)
def test_each_anomaly_scenario_hits_its_analyzer(
    golden_session, golden_projects, scenario: str, expected_analyzer: str
):
    project_ids, uploads_dir = golden_projects
    outcome, _ = run_scenario(
        scenario,
        session=golden_session,
        project_id=project_ids[scenario],
        uploads_dir=uploads_dir,
    )
    assert expected_analyzer in outcome.anomaly_types, (
        f"{scenario} 期望命中 {expected_analyzer}，实际 {outcome.anomaly_types}"
    )


def test_malformed_scenario_fails_explicitly_not_silently(golden_session, golden_projects):
    """计划第 960 行：坏格式期望**明确失败而非假报告**。"""
    project_ids, uploads_dir = golden_projects
    outcome, _ = run_scenario(
        "malformed",
        session=golden_session,
        project_id=project_ids["malformed"],
        uploads_dir=uploads_dir,
    )
    # 坏行被计数（明确失败）
    assert outcome.bad_lines > 0, "坏行没有被计数 —— 那就是静默失败"
    # 合法行没有被拖垮（计划第 531 行）
    assert outcome.parsed > 0, "合法行也被一起丢了"


def test_no_scenario_produces_a_fact_without_evidence(golden_session, golden_projects):
    """红线 3 在整轮 Golden Run 上成立。"""
    project_ids, uploads_dir = golden_projects
    report = run_all(session=golden_session, project_ids=project_ids, uploads_dir=uploads_dir)
    for outcome in report.outcomes:
        assert outcome.facts_without_evidence == 0, (
            f"{outcome.scenario} 产出了无证据的 fact"
        )


def test_report_table_is_renderable(golden_session, golden_projects):
    """表格是人工核对 Golden Set 的主要界面（计划第 965 行）。"""
    project_ids, uploads_dir = golden_projects
    report = run_all(session=golden_session, project_ids=project_ids, uploads_dir=uploads_dir)
    table = report.render_table()
    for scenario in SCENARIOS:
        assert scenario in table
    assert "断言" in table
