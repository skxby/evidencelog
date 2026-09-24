"""对象边界的字面量常量：7 态状态机、event_type、severity 等。

**本模块是这些取值集合的唯一真源。** 模型层的 CheckConstraint 与后续阶段的
校验逻辑都必须引用这里的常量，不许在各处重复写字符串字面量。

边界纪律（AGENTS.md 硬约束 6）：
- 下面标了「计划冻结」的集合，改动等于改对象边界 —— **必须先问，不许自己加值**。
- 标了「计划未定义」的字段，本阶段只落 VARCHAR 列、不加 CheckConstraint；
  取值集合等对应阶段（03/04/08/09）确定后再回填到本模块并补迁移。

引用出处：《V1 编码计划》§7 第 313–365 行、§13 第 683–693 行、§9 第 812 行。
"""

from __future__ import annotations

from typing import Final

# ============================================================
# AgentRun.status —— 计划冻结的 7 态状态机（§13 第 683–693 行）
# ============================================================

AGENT_RUN_QUEUED: Final = "queued"
AGENT_RUN_RUNNING: Final = "running"
AGENT_RUN_COMPLETED: Final = "completed"
AGENT_RUN_PARTIAL_SUCCESS: Final = "partial_success"
AGENT_RUN_FAILED: Final = "failed"
AGENT_RUN_TIMEOUT: Final = "timeout"
AGENT_RUN_CANCELLED: Final = "cancelled"

AGENT_RUN_STATES: Final[tuple[str, ...]] = (
    AGENT_RUN_QUEUED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_COMPLETED,
    AGENT_RUN_PARTIAL_SUCCESS,
    AGENT_RUN_FAILED,
    AGENT_RUN_TIMEOUT,
    AGENT_RUN_CANCELLED,
)

#: 终态：不可再改（计划第 691 行）
AGENT_RUN_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        AGENT_RUN_COMPLETED,
        AGENT_RUN_PARTIAL_SUCCESS,
        AGENT_RUN_FAILED,
        AGENT_RUN_TIMEOUT,
        AGENT_RUN_CANCELLED,
    }
)

#: 可从 queued 直接取消（计划第 688 行）
AGENT_RUN_CANCELLABLE_STATES: Final[frozenset[str]] = frozenset(
    {AGENT_RUN_QUEUED, AGENT_RUN_RUNNING}
)

#: failed 不可原地复活，只能新建 Run（计划第 692 行）
AGENT_RUN_REVIVABLE_STATES: Final[frozenset[str]] = frozenset()


def agent_run_check_sql(column: str = "status") -> str:
    """生成 CheckConstraint 用的 SQL 字面量。

    单一来源：模型与迁移都调它，避免两处字面量漂移。
    `column` 可传 `"agent_runs.status"`，让约束名在不同表间唯一。
    """
    return f"{column} IN (" + ", ".join(f"'{s}'" for s in AGENT_RUN_STATES) + ")"


def is_terminal_agent_run_status(status: str) -> bool:
    return status in AGENT_RUN_TERMINAL_STATES


# ============================================================
# Event.event_type —— 计划冻结两种（§7 第 348–349 行）
# ============================================================

EVENT_TYPE_LOG: Final = "log"
EVENT_TYPE_METRIC: Final = "metric"

EVENT_TYPES: Final[tuple[str, ...]] = (EVENT_TYPE_LOG, EVENT_TYPE_METRIC)

#: 指标事件约定的 payload 键（计划第 349 行给出示例）
METRIC_PAYLOAD_KEYS: Final[tuple[str, ...]] = ("metric_name", "value", "unit")


def event_type_check_sql(column: str = "event_type") -> str:
    """同 agent_run_check_sql：`column` 可传 `表.列` 让约束名唯一。"""
    return f"{column} IN (" + ", ".join(f"'{t}'" for t in EVENT_TYPES) + ")"


# ============================================================
# severity —— 计划冻结三档（§7 第 98 行 Incident 归并规则、§9 第 812 行）
# ============================================================

SEVERITY_HIGH: Final = "high"
SEVERITY_MEDIUM: Final = "medium"
SEVERITY_LOW: Final = "low"

SEVERITIES: Final[tuple[str, ...]] = (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)


def severity_check_sql(column: str = "severity") -> str:
    return f"{column} IN (" + ", ".join(f"'{s}'" for s in SEVERITIES) + ")"


# ============================================================
# Insight.type —— 计划第 811、829–836 行的**四层语义**
# ============================================================

INSIGHT_TYPE_FACT: Final = "fact"
INSIGHT_TYPE_INFERENCE: Final = "inference"
INSIGHT_TYPE_POSSIBILITY: Final = "possibility"
#: 计划第 836 行：「− 未知」+ 信息缺口。第四层与 possibility 的区别是
#: 「不确定是不是这个原因」vs「信息不足，连候选都提不出来」，报告页要分开渲染，
#: 故数据库也必须能存 —— 约束里少这一个值，模型按 schema 给出的合法结论
#: 会在落库时被 CHECK 拒掉（真机实测发生过）。
INSIGHT_TYPE_UNKNOWN: Final = "unknown"

INSIGHT_TYPES: Final[tuple[str, ...]] = (
    INSIGHT_TYPE_FACT,
    INSIGHT_TYPE_INFERENCE,
    INSIGHT_TYPE_POSSIBILITY,
    INSIGHT_TYPE_UNKNOWN,
)

#: 计划第 1112 行：根因只能标 inference / possibility，不能标 fact
INSIGHT_ROOT_CAUSE_ALLOWED_TYPES: Final[frozenset[str]] = frozenset(
    {INSIGHT_TYPE_INFERENCE, INSIGHT_TYPE_POSSIBILITY}
)


def insight_type_check_sql(column: str = "type") -> str:
    """注意列名是 `type`（计划列名如此），故默认参数不是 `insight_type`。"""
    return f"{column} IN (" + ", ".join(f"'{t}'" for t in INSIGHT_TYPES) + ")"


# ============================================================
# DataSource.format —— V1 只支持两种（AGENTS.md 技术栈、§7 第 320 行）
# ============================================================

FORMAT_TXT: Final = "txt"
FORMAT_JSONL: Final = "jsonl"

DATA_SOURCE_FORMATS: Final[tuple[str, ...]] = (FORMAT_TXT, FORMAT_JSONL)

# 注意：§20 不做清单明确把 CSV / JSON 排除在 V1 之外，故这里不列。


# ============================================================
# AgentRun.current_phase —— 流水线阶段名（§7 第 329 行给出的例子）
# ============================================================

PHASE_PARSE: Final = "parse"
PHASE_GROUP: Final = "group"
PHASE_ANALYZE: Final = "analyze"
PHASE_MODEL: Final = "model"
PHASE_VALIDATE: Final = "validate"

PIPELINE_PHASES: Final[tuple[str, ...]] = (
    PHASE_PARSE,
    PHASE_GROUP,
    PHASE_ANALYZE,
    PHASE_MODEL,
    PHASE_VALIDATE,
)

# 注意：§7 用「如」举例，未声明这是封闭集合，故**不加** CheckConstraint。
# 阶段 09 编排落地时若确认封闭，再回来补约束与迁移。


# ============================================================
# 计划未定义的字段 —— 只落 VARCHAR，不加约束（见模块 docstring）
# ============================================================

# 以下字段计划只给了列名，没有给出取值集合。本阶段**刻意不猜**：
#   Project.status / DataSource.type / DataSource.status / EventGroup.status /
#   Incident.status
# 它们的约束等对应阶段确定后再补。任何人想在这里加值，先确认来源。
UNCONSTRAINED_STATUS_FIELDS: Final[tuple[str, ...]] = (
    "Project.status",
    "DataSource.type",
    "DataSource.status",
    "EventGroup.status",
    "Incident.status",
)
