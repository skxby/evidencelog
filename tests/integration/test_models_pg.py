"""阶段 02 验收的集成测试（真实 PostgreSQL）。

逐条对应《V1 编码计划》第 365 行的验收：
  1. 迁移可执行可回滚
  2. 能创建 Project / DataSource
  3. 能插入普通事件与指标事件并按 project 查回
  4. 事件可归入 EventGroup、再归并为 Incident 并查回
  5. model_calls 可追加记录
外加：Project 隔离（计划第 363 行「隔离测试见阶段 13」，这里先立住机制）。

需要先 `docker compose up -d` 并 `alembic upgrade head`。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.models import (
    DataSource,
    Event,
    Evidence,
    Insight,
    Project,
    User,
    enums,
)
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    EventGroupRepository,
    EventRepository,
    EvidenceRepository,
    IncidentRepository,
    InsightRepository,
    ProjectRepository,
    UserRepository,
)

pytestmark = pytest.mark.integration

UTC = timezone.utc


# ============================================================
# 验收 1：迁移可执行可回滚
# ============================================================


def test_migration_created_all_nine_business_tables():
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename <> 'alembic_version' "
                "ORDER BY tablename"
            )
        ).scalars().all()
    assert set(rows) == {
        "agent_runs",
        "data_sources",
        "event_groups",
        "events",
        "evidences",
        "incidents",
        "insights",
        "projects",
        "users",
    }


def test_alembic_revision_is_at_head():
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version, "alembic_version 为空：迁移没跑"


def test_deferred_circular_fk_exists_in_database():
    """Event ↔ Incident 的环形外键必须真的建出来了。

    这条是有来历的：`use_alter=True` 只对 metadata.create_all() 生效，
    Alembic 的 op.create_table() 会**静默忽略**它，导致该外键凭空消失。
    """
    with engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname = 'fk_events_incident_id_incidents'"
            )
        ).scalars().all()
    assert found == ["fk_events_incident_id_incidents"]


# ============================================================
# 夹具：两个独立项目，用于验收 2–5 与隔离测试
# ============================================================


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def two_projects(session):
    """建两个项目，返回 (user, project_a, project_b)。

    只 flush 不 commit —— 测试结束 rollback，库里不留垃圾数据。
    """
    users = UserRepository(session)
    user = users.add(
        User(email=f"stage02-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()

    projects = ProjectRepository(session)
    pa = projects.add(Project(user_id=user.id, name="proj-a", budget_total=10, budget_used=0))
    pb = projects.add(Project(user_id=user.id, name="proj-b", budget_total=10, budget_used=0))
    session.flush()
    return user, pa, pb


# ============================================================
# 验收 2：能创建 Project / DataSource
# ============================================================


def test_create_project_and_datasource(session, two_projects):
    _, pa, _ = two_projects
    sources = DataSourceRepository(session)
    src = sources.add(
        pa.id,
        DataSource(
            project_id=pa.id,
            type="file_upload",
            format=enums.FORMAT_TXT,
            location="uploads/a.log",
            meta={"note": "阶段 02 验收"},
        ),
    )
    session.flush()

    assert src.id is not None
    fetched = sources.get(pa.id, src.id)
    assert fetched is not None
    assert fetched.format == enums.FORMAT_TXT
    assert fetched.meta == {"note": "阶段 02 验收"}


# ============================================================
# 验收 3：普通事件与指标事件，按 project 查回
# ============================================================


def test_insert_log_and_metric_events_and_query_by_project(session, two_projects):
    _, pa, pb = two_projects
    sources = DataSourceRepository(session)
    events = EventRepository(session)

    src_a = sources.add(
        pa.id,
        DataSource(project_id=pa.id, type="file_upload", format=enums.FORMAT_TXT, location="a.log"),
    )
    src_b = sources.add(
        pb.id,
        DataSource(project_id=pb.id, type="file_upload", format=enums.FORMAT_TXT, location="b.log"),
    )
    session.flush()

    base = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    # 普通事件
    events.add(
        pa.id,
        Event(
            project_id=pa.id,
            source_id=src_a.id,
            timestamp=base,
            event_type=enums.EVENT_TYPE_LOG,
            severity=enums.SEVERITY_HIGH,
            message="sshd: authentication failure",
        ),
    )
    # 指标事件（指标不单独建表）
    events.add_metric(
        pa.id,
        source_id=src_a.id,
        timestamp=base + timedelta(seconds=1),
        metric_name="cpu_used",
        value=92.4,
        unit="%",
        severity=enums.SEVERITY_MEDIUM,
    )
    # 别的项目的数据
    events.add(
        pb.id,
        Event(
            project_id=pb.id,
            source_id=src_b.id,
            timestamp=base,
            event_type=enums.EVENT_TYPE_LOG,
            severity=enums.SEVERITY_LOW,
            message="belongs to project b",
        ),
    )
    session.flush()

    a_logs = events.list_log_events(pa.id)
    a_metrics = events.list_metric_events(pa.id)

    assert len(a_logs) == 1 and a_logs[0].message.startswith("sshd")
    assert len(a_metrics) == 1
    assert a_metrics[0].payload == {"metric_name": "cpu_used", "value": 92.4, "unit": "%"}
    assert events.count(pa.id) == 2  # 看不到 project b 的那条
    assert events.count_by_severity(pa.id) == {"high": 1, "medium": 1}


# ============================================================
# 验收 4：EventGroup → Event.group_id → Incident.group_ids
# ============================================================


def test_group_events_then_merge_into_incident(session, two_projects):
    _, pa, _ = two_projects
    sources = DataSourceRepository(session)
    events = EventRepository(session)
    groups = EventGroupRepository(session)
    incidents = IncidentRepository(session)

    src = sources.add(
        pa.id,
        DataSource(project_id=pa.id, type="file_upload", format=enums.FORMAT_TXT, location="a.log"),
    )
    session.flush()

    base = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    # 3 条同模板事件 → 1 个分组
    group = groups.create_group(
        pa.id,
        source_id=src.id,
        group_key="tmpl-cpu-high",
        template="cpu usage <NUM>% on <HOST>",
        time_start=base,
        time_end=base + timedelta(minutes=1),
    )
    session.flush()

    group_events = [
        events.add(
            pa.id,
            Event(
                project_id=pa.id,
                source_id=src.id,
                group_id=group.id,
                timestamp=base + timedelta(seconds=i),
                event_type=enums.EVENT_TYPE_LOG,
                severity=enums.SEVERITY_HIGH,
                message=f"cpu usage 9{i}% on host1",
            ),
        )
        for i in range(3)
    ]
    session.flush()
    groups.set_event_count(pa.id, group.id, 3)
    session.flush()

    # 按 Event.group_id 查成员（真源查法，不是反向数组）
    members = events.list_by_group(pa.id, group.id)
    assert len(members) == 3
    assert groups.get(pa.id, group.id).event_count == 3

    # 分组 → 事故
    incident = incidents.create_incident(
        pa.id,
        title="CPU 持续高位",
        severity=enums.SEVERITY_HIGH,
        group_ids=[group.id],
        time_start=base,
        time_end=base + timedelta(minutes=1),
        root_cause=None,
    )
    session.flush()

    # 事件挂到事故上
    for e in group_events:
        e.incident_id = incident.id
    session.flush()

    assert incidents.group_ids_of(pa.id, incident.id) == [group.id]
    # 成员事件由 group_ids 派生（不存冗余 event_ids）
    derived = incidents.event_ids_of(pa.id, incident.id)
    assert sorted(derived) == sorted(e.id for e in group_events)


# ============================================================
# 验收 5：model_calls 可追加记录
# ============================================================


def test_append_model_calls(session, two_projects):
    from app.models import AgentRun

    _, pa, _ = two_projects
    sources = DataSourceRepository(session)
    runs = AgentRunRepository(session)

    src = sources.add(
        pa.id,
        DataSource(project_id=pa.id, type="file_upload", format=enums.FORMAT_TXT, location="a.log"),
    )
    session.flush()

    run = runs.add(
        pa.id,
        AgentRun(project_id=pa.id, source_id=src.id, status=enums.AGENT_RUN_QUEUED),
    )
    session.flush()

    assert runs.append_model_call(
        pa.id,
        run.id,
        {
            "call_id": "c1",
            "tier": "L1",
            "model": "m",
            "tokens_input": 100,
            "tokens_output": 20,
            "cost": 0.001,
            "status": "ok",
        },
    )
    assert runs.append_model_call(
        pa.id,
        run.id,
        {
            "call_id": "c2",
            "tier": "L3",
            "model": "m",
            "tokens_input": 800,
            "tokens_output": 200,
            "cost": 0.004,
            "status": "ok",
        },
    )
    session.flush()
    session.expire_all()

    calls = runs.model_calls_of(pa.id, run.id)
    assert len(calls) == 2
    assert [c["call_id"] for c in calls] == ["c1", "c2"]
    assert calls[1]["tokens_output"] == 200


# ============================================================
# Project 隔离：拿不到别家数据
# ============================================================


def test_repository_refuses_cross_project_read(session, two_projects):
    _, pa, pb = two_projects
    sources = DataSourceRepository(session)
    src_b = sources.add(
        pb.id,
        DataSource(project_id=pb.id, type="file_upload", format=enums.FORMAT_TXT, location="b.log"),
    )
    session.flush()

    # 用 A 的身份查 B 的 DataSource：必须拿不到
    assert sources.get(pa.id, src_b.id) is None
    assert sources.get(pb.id, src_b.id) is not None
    assert sources.count(pa.id) == 0


def test_repository_refuses_cross_project_write(session, two_projects):
    _, pa, pb = two_projects
    sources = DataSourceRepository(session)
    with pytest.raises(ValueError, match="不一致"):
        sources.add(
            pa.id,
            DataSource(
                project_id=pb.id, type="file_upload", format=enums.FORMAT_TXT, location="x"
            ),
        )


def test_evidence_isolation_goes_through_insight(session, two_projects):
    """Evidence 没有 project_id，隔离必须经 insights 关联实现。"""
    from app.models import AgentRun

    _, pa, pb = two_projects
    sources = DataSourceRepository(session)
    runs = AgentRunRepository(session)
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    src = sources.add(
        pa.id,
        DataSource(project_id=pa.id, type="file_upload", format=enums.FORMAT_TXT, location="a.log"),
    )
    session.flush()
    run = runs.add(pa.id, AgentRun(project_id=pa.id, source_id=src.id))
    session.flush()
    insight = insights.add(
        pa.id,
        Insight(
            project_id=pa.id,
            run_id=run.id,
            type=enums.INSIGHT_TYPE_FACT,
            severity=enums.SEVERITY_HIGH,
            confidence=0.9,
            title="cpu high",
            summary="s",
        ),
    )
    session.flush()
    evidences.add(
        pa.id,
        Evidence(insight_id=insight.id, event_ids=[1, 2], description="d"),
    )
    session.flush()

    assert len(evidences.list_for_insight(pa.id, insight.id)) == 1
    # 用 B 的身份查，拿不到
    assert evidences.list_for_insight(pb.id, insight.id) == []


def test_evidence_insert_rejects_mismatched_project(session, two_projects):
    from app.models import AgentRun

    _, pa, pb = two_projects
    sources = DataSourceRepository(session)
    runs = AgentRunRepository(session)
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    src = sources.add(
        pa.id,
        DataSource(project_id=pa.id, type="file_upload", format=enums.FORMAT_TXT, location="a.log"),
    )
    session.flush()
    run = runs.add(pa.id, AgentRun(project_id=pa.id, source_id=src.id))
    session.flush()
    insight = insights.add(
        pa.id,
        Insight(
            project_id=pa.id,
            run_id=run.id,
            type=enums.INSIGHT_TYPE_FACT,
            severity=enums.SEVERITY_HIGH,
            confidence=0.5,
            title="t",
            summary="s",
        ),
    )
    session.flush()

    with pytest.raises(ValueError, match="不一致"):
        evidences.add(
            pb.id,
            Evidence(insight_id=insight.id, event_ids=[1], description="d"),
        )
