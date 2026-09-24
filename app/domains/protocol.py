"""Domain 插件契约。

设计要点（对应计划 §8 第 373–402 行）：

- `DomainBase` 是**插件契约**，不是运行时。核心 Runtime 只认这里的类型，
  因此本模块**不 import `app.models`** —— 领域不该依赖数据库层。领域产出
  `NormalizedEvent`，由上传/解析管道（阶段 04）落库。这条边界让「加领域不改内核」
  在类型层面就成立。
- `tool` 与 `analyzer` 的边界（计划第 396–402 行）：
    tool     通用数据算子：过滤 / 统计 / 时间窗口，跨领域复用，**不含领域判断**
    analyzer 领域检测：含阈值与判断逻辑，是 domain 私有资产，产出候选异常
  不要把阈值判断写进 tool，也不要把通用算子写进 analyzer。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EventType = Literal["log", "metric"]
Severity = Literal["high", "medium", "low"]

#: `parse_line` 的返回值：原始字段字典，字段名由各 domain 自定。
RawRecord = dict[str, Any]


# ============================================================
# 领域产出的值对象
# ============================================================


@dataclass(frozen=True)
class NormalizedEvent:
    """领域归一化后的标准事件。

    刻意不是 ORM 对象：domain 层不依赖 `app.models`（见模块 docstring）。
    阶段 04 的管道负责把它映射成 `Event` 行。
    """

    timestamp: datetime
    message: str
    event_type: EventType = "log"
    severity: Severity = "low"
    #: 指标事件的载体，如 {"metric_name": "cpu_used", "value": 92.4, "unit": "%"}
    payload: dict[str, Any] | None = None
    #: 原始字段，便于排障与回溯
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricSpec:
    """声明「本领域识别哪些指标、如何从原始字段抽取」。

    计划第 387 行：`metric_specs()` 识别哪些指标、如何抽取。
    """

    metric_name: str
    unit: str
    #: 从原始字段里抽取数值的键名
    source_key: str


@dataclass(frozen=True)
class AnalyzerFinding:
    """analyzer 的产出：一条**候选异常**。

    注意措辞是「候选」：它只是确定性规则的命中结果，不是根因结论。
    """

    analyzer: str
    severity: Severity
    #: 人类可读的说明，会进入报告
    message: str
    #: 命中的指标名（若与指标有关）
    metric_name: str | None = None
    #: 命中的事件 id 列表，供 Evidence 校验（红线 3）
    event_ids: list[int] = field(default_factory=list)
    #: 供报告展示的量化依据，如 {"value": 92.4, "threshold": 90}
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Runbook:
    """可执行 procedure（YAML 加载）。

    计划第 412 行强调：runbook 是**可执行检查清单**，不是知识库文档。
    V1 只读展示，不自动执行（只读定位）。
    """

    id: str
    title: str
    applies: dict[str, Any]
    steps: list[str]
    references: list[str] = field(default_factory=list)


#: 知识类别白名单 —— **领域契约的一部分**，不是某个领域包的私事。
#: 放在协议层是因为两侧都要用：领域加载器按它校验 YAML，
#: 而核心 Runtime 在把模型候选写进 staging 之前必须归一化到它
#: （Runtime 不能 import 具体领域，红线 8）。
VALID_KNOWLEDGE_KINDS: tuple[str, ...] = (
    "error_pattern",
    "root_cause_hint",
    "fix_suggestion",
    "false_positive",
)


@dataclass(frozen=True)
class KnowledgeEntry:
    """一条领域知识（计划第 455–475 行的结构）。

    约束（计划第 491–496 行）：
    - `status="draft"` 的知识**不参与自动结论**，只作提示；
    - 无 evidence、无 confidence 的条目**不允许** confirmed。
    """

    id: str
    kind: Literal["error_pattern", "root_cause_hint", "fix_suggestion", "false_positive"]
    title: str
    match: dict[str, Any] = field(default_factory=dict)
    message_pattern: str | None = None
    severity_hint: Severity | None = None
    description: str = ""
    related: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    status: Literal["draft", "confirmed", "deprecated"] = "draft"
    created_at: str = ""
    updated_at: str = ""


class KnowledgeNotConfirmableError(ValueError):
    """条目缺少 evidence / confidence 时不允许确认（计划第 494 行）。"""


# ============================================================
# 插件契约
# ============================================================


class DomainBase(ABC):
    """一个领域 = 一个自包含目录包（计划 §3.3 第 159–168 行）。

    核心 Runtime 只通过本接口访问领域；**任何 `if domain == "..."` 都是违规**。
    """

    domain_id: str
    version: str

    @abstractmethod
    def parse_line(self, line: str) -> RawRecord | None:
        """原始行 → 原始字段；无法解析返回 None（**不抛异常、不猜**）。"""

    @abstractmethod
    def normalize(self, raw: RawRecord) -> NormalizedEvent:
        """原始字段 → 标准事件（普通事件或指标事件）。"""

    @abstractmethod
    def metric_specs(self) -> list[MetricSpec]:
        """本领域识别哪些指标、如何抽取。"""

    @abstractmethod
    def analyzers(self) -> list[Any]:
        """手写确定性检测器（统一称 analyzer）。

        计划第 390 行：手写确定性检测（LLM 不参与判断）—— 红线 1。
        """

    @abstractmethod
    def runbooks(self) -> list[Runbook]:
        """可执行 procedure（YAML 加载）。"""

    def knowledge_dir(self) -> Any:
        """领域知识目录（confirmed / staging）。

        V1 以文件为知识载体、不建知识表（计划第 438–451 行）。
        """
        from pathlib import Path

        return Path(__file__).resolve().parent
