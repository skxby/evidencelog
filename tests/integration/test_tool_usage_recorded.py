"""阶段 05 验收的集成测试（真实 PostgreSQL）。

验收最后一项：「执行结果可记录到 Run」——这条必须真的落库验证，
不能只在单测里断言 `ToolResult` 长得对。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import AgentRun, DataSource, Project, User
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    ProjectRepository,
    UserRepository,
)
from app.tools import get_tool_registry, reset_tool_registry

pytestmark = pytest.mark.integration
UTC = timezone.utc
BASE = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def run_in_db(session):
    users = UserRepository(session)
    user = users.add(
        User(email=f"stage05-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    project = ProjectRepository(session).add(
        Project(user_id=user.id, name="p05", budget_total=10, budget_used=0)
    )
    session.flush()
    source = DataSourceRepository(session).add(
        project.id,
        DataSource(project_id=project.id, type="file_upload", format="txt", location="a.log"),
    )
    session.flush()
    runs = AgentRunRepository(session)
    run = runs.add(project.id, AgentRun(project_id=project.id, source_id=source.id))
    session.flush()
    return project, runs, run


@pytest.fixture()
def registry():
    reset_tool_registry()
    reg = get_tool_registry()
    yield reg
    reset_tool_registry()


def sample_events() -> list[dict]:
    return [
        {"event_id": 1, "timestamp": BASE, "severity": "high", "event_type": "log", "message": "boom"},
        {
            "event_id": 2,
            "timestamp": BASE + timedelta(seconds=30),
            "severity": "low",
            "event_type": "metric",
            "message": "cpu 20%",
        },
        {
            "event_id": 3,
            "timestamp": BASE + timedelta(seconds=90),
            "severity": "medium",
            "event_type": "log",
            "message": "warning",
        },
    ]


def test_tool_results_can_be_recorded_to_run(session, run_in_db, registry):
    """验收：三个工具的执行结果都能记进 AgentRun.tool_usage 并读回。"""
    project, runs, run = run_in_db

    for name, kwargs in (
        ("stats_calculator", {"events": sample_events()}),
        ("event_filter", {"events": sample_events(), "severities": ["high"]}),
        ("time_window", {"events": sample_events(), "window_seconds": 60}),
    ):
        result = registry.call(name, **kwargs)
        assert result.ok, f"{name} 执行失败：{result.error}"
        assert runs.append_tool_usage(project.id, run.id, result.as_dict()) is True

    session.flush()
    session.expire_all()

    usage = runs.tool_usage_of(project.id, run.id)
    assert set(usage) == {"stats_calculator", "event_filter", "time_window"}
    assert usage["stats_calculator"][0]["output"]["total"] == 3
    assert usage["event_filter"][0]["output"]["matched_count"] == 1
    assert usage["time_window"][0]["output"]["window_count"] == 2


def test_repeated_calls_accumulate_in_history(session, run_in_db, registry):
    """同一工具多次执行要按顺序累积，不是覆盖。"""
    project, runs, run = run_in_db
    for _ in range(3):
        result = registry.call("stats_calculator", events=sample_events())
        runs.append_tool_usage(project.id, run.id, result.as_dict())
    session.flush()
    session.expire_all()

    history = runs.tool_usage_of(project.id, run.id)["stats_calculator"]
    assert len(history) == 3
    assert all(entry["ok"] for entry in history)


def test_failed_tool_result_is_also_recorded(session, run_in_db, registry):
    """失败也要留痕 —— 否则「跑过但失败了」会从记录里消失（红线 4）。"""
    project, runs, run = run_in_db
    bad = registry.call("stats_calculator", events="not a list")
    assert bad.ok is False

    assert runs.append_tool_usage(project.id, run.id, bad.as_dict()) is True
    session.flush()
    session.expire_all()

    entry = runs.tool_usage_of(project.id, run.id)["stats_calculator"][0]
    assert entry["ok"] is False
    assert "ToolInputError" in entry["error"]
    assert "output" not in entry


def test_unserializable_tool_usage_is_refused(session, run_in_db):
    """不可 JSON 序列化的内容必须明确报错，不能悄悄进库。"""
    project, runs, run = run_in_db
    with pytest.raises(ValueError, match="不可 JSON 序列化"):
        runs.append_tool_usage(
            project.id, run.id, {"tool": "x", "ok": True, "output": {"at": datetime.now(UTC)}}
        )


def test_tool_usage_respects_project_isolation(session, run_in_db, registry):
    """别的项目写不进这个 Run，也读不到它的用量。"""
    _project, runs, run = run_in_db

    users = UserRepository(session)
    other_user = users.add(
        User(email=f"other05-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    other_project = ProjectRepository(session).add(
        Project(user_id=other_user.id, name="other", budget_total=1, budget_used=0)
    )
    session.flush()

    assert runs.append_tool_usage(other_project.id, run.id, {"tool": "x", "ok": True}) is False
    assert runs.tool_usage_of(other_project.id, run.id) == {}
