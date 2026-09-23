"""模型结构单测：钉死 9 张表与对象边界（防返工）。

这些断言的意义：修订说明第 3/4 条把真源、字段增删都定死了。
谁在后续阶段"顺手"加回 event_ids 或把 phase 混进 status，这里立刻红。
"""

from __future__ import annotations

import pytest

from app.models import (
    MODELS,
    AgentRun,
    Base,
    Event,
    EventGroup,
    Evidence,
    Incident,
    Insight,
)

EXPECTED_TABLES = {
    "users",
    "projects",
    "data_sources",
    "events",
    "event_groups",
    "incidents",
    "agent_runs",
    "insights",
    "evidences",
}


def test_exactly_nine_tables():
    """9 张表是冻结边界；新增第 10 张必须先问（硬约束 6）。"""
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert len(MODELS) == 9


def test_every_model_class_is_registered():
    assert {m.__tablename__ for m in MODELS} == EXPECTED_TABLES


# ---------- 防返工：真源不能有反向数组 ----------


def test_event_group_has_no_event_ids_reverse_array():
    """修订说明第 4 条：真源是 Event.group_id，EventGroup 不存 event_ids。"""
    assert "event_ids" not in EventGroup.__table__.c


def test_incident_has_no_redundant_event_ids():
    """修订说明第 4 条：Incident 真源是 group_ids，不存冗余 event_ids。"""
    assert "event_ids" not in Incident.__table__.c
    assert "group_ids" in Incident.__table__.c


def test_event_group_id_is_the_single_grouping_source():
    assert "group_id" in Event.__table__.c
    fk_targets = {fk.target_fullname for fk in Event.__table__.c.group_id.foreign_keys}
    assert fk_targets == {"event_groups.id"}


def test_evidence_event_ids_is_allowed():
    """Evidence.event_ids 是计划明确要有的（它记录的是证据，不是反向索引）。"""
    assert "event_ids" in Evidence.__table__.c


# ---------- 防返工：9 表之外不该有的表 ----------


@pytest.mark.parametrize(
    "forbidden",
    ["metrics", "model_calls", "tasks", "domain_versions", "policies"],
)
def test_no_extra_tables_from_plan_negative_list(forbidden):
    """计划第 348–357 行：这些明确不建表。"""
    assert forbidden not in Base.metadata.tables


# ---------- Project 边界 ----------


@pytest.mark.parametrize(
    "model",
    [Event, EventGroup, Incident, AgentRun, Insight],
)
def test_business_tables_carry_project_id(model):
    assert "project_id" in model.__table__.c


def test_user_has_no_project_id():
    """User 是账号，不属于任何 project。"""
    from app.models import User

    assert "project_id" not in User.__table__.c


def test_evidence_reaches_project_through_insight():
    """Evidence 无 project_id（计划表结构如此），靠 insight_id 归属。"""
    assert "project_id" not in Evidence.__table__.c
    assert "insight_id" in Evidence.__table__.c


# ---------- status 与 phase 正交 ----------


def test_agent_run_status_and_phase_are_separate_columns():
    """修订说明第 3 条：status 管 7 态，current_phase/phase_history 管进度，正交。"""
    assert "status" in AgentRun.__table__.c
    assert "current_phase" in AgentRun.__table__.c
    assert "phase_history" in AgentRun.__table__.c
    assert "phase" not in AgentRun.__table__.c


def test_agent_run_has_heartbeat_and_model_calls():
    """阶段 08 的僵尸回收依赖 last_heartbeat；验收 5 依赖 model_calls。"""
    assert "last_heartbeat" in AgentRun.__table__.c
    assert "model_calls" in AgentRun.__table__.c


# ---------- metadata 保留名规避 ----------


def test_metadata_columns_map_to_meta_attribute():
    """列名保持 metadata，ORM 属性名必须是 meta（metadata 是声明式保留名）。"""
    from app.models import DataSource

    for model in (Event, DataSource):
        column = model.__table__.c["metadata"]
        assert column.name == "metadata"
    assert hasattr(Event, "meta")
    assert hasattr(DataSource, "meta")


# ---------- 时间列统一带时区 ----------


def test_timestamp_columns_are_timezone_aware():
    """时区感知列是「存 UTC」的前提，naive 时间会造成静默错误。"""
    assert Event.__table__.c.timestamp.type.timezone is True
    assert AgentRun.__table__.c.started_at.type.timezone is True
    assert AgentRun.__table__.c.last_heartbeat.type.timezone is True


# ---------- 环形外键 ----------


def test_events_incident_fk_is_deferred_with_use_alter():
    """Event ↔ Incident 互相引用，该外键必须延迟创建。"""
    (fk,) = tuple(Event.__table__.c.incident_id.foreign_keys)
    assert fk.target_fullname == "incidents.id"
    assert fk.use_alter is True


def test_every_table_has_a_primary_key():
    """Alembic 与 ORM 都要求主键；漏了会在运行时才炸。"""
    for name, table in Base.metadata.tables.items():
        assert table.primary_key.columns, f"{name} 缺少主键"


def test_high_volume_tables_use_bigint_ids():
    """Event / EventGroup / Insight / Evidence 是增长最快的表，用 BigInteger。"""
    from sqlalchemy import BigInteger

    for model in (Event, EventGroup, Insight, Evidence):
        assert isinstance(model.__table__.c.id.type, BigInteger), model.__tablename__


def test_created_at_present_only_where_the_plan_declares_it():
    """`created_at` 只加在计划声明了它的表上。

    Event 刻意**没有** created_at：它带的是日志自身的时间戳 `timestamp`，
    「入库时间」在阶段 02 的表结构里不存在，不要顺手加第 12 列。
    """
    for model in (EventGroup, Incident, Insight):
        assert "created_at" in model.__table__.c, model.__tablename__
    assert "created_at" not in Event.__table__.c
    assert "created_at" not in AgentRun.__table__.c


def test_agent_run_uses_lifecycle_timestamps_not_created_at():
    """AgentRun 用 started_at / finished_at / last_heartbeat 表达生命周期。"""
    for column in ("started_at", "finished_at", "last_heartbeat"):
        assert column in AgentRun.__table__.c
