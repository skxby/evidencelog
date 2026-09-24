"""对象边界常量单测。

这些集合一旦漂移就会让「9 表 / 7 态」失去意义，所以逐条钉死。
"""

from __future__ import annotations

from app.models import enums


def test_agent_run_has_exactly_seven_states():
    """计划 §13 冻结的 7 态。多一个少一个都是改对象边界。"""
    assert len(enums.AGENT_RUN_STATES) == 7
    assert set(enums.AGENT_RUN_STATES) == {
        "queued",
        "running",
        "completed",
        "partial_success",
        "failed",
        "timeout",
        "cancelled",
    }


def test_terminal_states_are_the_five_post_running_outcomes():
    """计划第 691 行：进入终态后不可再改。"""
    assert enums.AGENT_RUN_TERMINAL_STATES == {
        "completed",
        "partial_success",
        "failed",
        "timeout",
        "cancelled",
    }
    # queued / running 不是终态
    assert "queued" not in enums.AGENT_RUN_TERMINAL_STATES
    assert "running" not in enums.AGENT_RUN_TERMINAL_STATES


def test_terminal_and_non_terminal_partition_the_seven_states():
    non_terminal = set(enums.AGENT_RUN_STATES) - set(enums.AGENT_RUN_TERMINAL_STATES)
    assert non_terminal == {"queued", "running"}


def test_failed_is_not_revivable():
    """计划第 692 行：failed 不可原地复活，只能新建 Run。"""
    assert enums.AGENT_RUN_REVIVABLE_STATES == frozenset()
    assert enums.is_terminal_agent_run_status("failed") is True


def test_is_terminal_helper_matches_constant_set():
    for state in enums.AGENT_RUN_STATES:
        expected = state in enums.AGENT_RUN_TERMINAL_STATES
        assert enums.is_terminal_agent_run_status(state) is expected


def test_agent_run_check_sql_lists_all_seven_states():
    sql = enums.agent_run_check_sql()
    assert sql.startswith("status IN (")
    for state in enums.AGENT_RUN_STATES:
        assert f"'{state}'" in sql
    assert sql.count("'") == len(enums.AGENT_RUN_STATES) * 2


def test_agent_run_check_sql_accepts_qualified_column():
    """约束名在不同表间要唯一，故 support 传 `表.列`。"""
    sql = enums.agent_run_check_sql("agent_runs.status")
    assert sql.startswith("agent_runs.status IN (")
    assert "'queued'" in sql


def test_event_types_are_log_and_metric_only():
    """计划第 348–349 行：指标不单独建表，以 event_type='metric' 写入 Event。"""
    assert enums.EVENT_TYPES == ("log", "metric")
    assert enums.METRIC_PAYLOAD_KEYS == ("metric_name", "value", "unit")


def test_severities_are_three_tiers():
    """计划第 98 行 Incident 归并要求 severity 同档，故只允许三档。"""
    assert enums.SEVERITIES == ("high", "medium", "low")
    assert "'high'" in enums.severity_check_sql()
    assert "'high'" in enums.severity_check_sql("severity")


def test_insight_types_and_root_cause_restriction():
    """计划第 1112 行：根因只能标 inference / possibility，不能标 fact。"""
    assert "fact" in enums.INSIGHT_TYPES
    assert "inference" in enums.INSIGHT_TYPES
    assert "possibility" in enums.INSIGHT_TYPES
    assert enums.INSIGHT_TYPE_FACT not in enums.INSIGHT_ROOT_CAUSE_ALLOWED_TYPES
    assert enums.INSIGHT_ROOT_CAUSE_ALLOWED_TYPES == {"inference", "possibility"}


def test_unknown_is_a_storable_insight_type():
    """计划第 811、829–836 行的**第四层**语义必须能落库。

    真机故障：模型按 schema 给出 type="unknown" 的合法结论，而
    `insights.type` 的 CHECK 只认三值 → 插入被拒 → 任务崩溃。
    schema（第 811 行）、prompt、报告页（第 836 行「− 未知」）都已实现第四层，
    约束里少这一个值，等于把合法输出变成运行时错误。
    """
    assert enums.INSIGHT_TYPE_UNKNOWN == "unknown"
    assert "unknown" in enums.INSIGHT_TYPES
    assert "'unknown'" in enums.insight_type_check_sql()


def test_valid_insight_types_has_a_single_definition():
    """校验层与模型层必须共用同一份类型清单。

    抄两份就必然漂移：`evidence.py` 认 4 种、数据库约束只认 3 种，
    于是漂移只在**真机落库那一刻**才炸出来，单测全绿也没用。
    """
    from app.analysis.evidence import VALID_INSIGHT_TYPES

    assert VALID_INSIGHT_TYPES == enums.INSIGHT_TYPES


def test_data_source_formats_exclude_csv_and_json():
    """§20 不做清单：CSV / JSON 解析明确排除在 V1 之外。"""
    assert enums.DATA_SOURCE_FORMATS == ("txt", "jsonl")
    assert "csv" not in enums.DATA_SOURCE_FORMATS
    assert "json" not in enums.DATA_SOURCE_FORMATS


def test_pipeline_phases_are_recorded_but_not_frozen():
    """§7 用「如」举例，未声明封闭集合 —— 故常量存在但不加约束。"""
    assert enums.PIPELINE_PHASES == ("parse", "group", "analyze", "model", "validate")
    assert enums.PHASE_PARSE in enums.PIPELINE_PHASES


def test_unconstrained_fields_are_documented_not_guessed():
    """计划未给取值集合的字段必须被显式列出，提醒后人不要瞎猜。"""
    assert len(enums.UNCONSTRAINED_STATUS_FIELDS) == 5
    for field in enums.UNCONSTRAINED_STATUS_FIELDS:
        assert "." in field


def test_check_sql_helpers_are_single_source():
    """模型与迁移都该调这些 helper，避免字面量两处漂移。"""
    assert enums.agent_run_check_sql().count(",") == len(enums.AGENT_RUN_STATES) - 1
    assert enums.event_type_check_sql().count(",") == len(enums.EVENT_TYPES) - 1
    assert enums.insight_type_check_sql().count(",") == len(enums.INSIGHT_TYPES) - 1
