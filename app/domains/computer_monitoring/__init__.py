"""`ComputerMonitoringDomain` —— 把解析、指标、检测器、runbook 组装成一个目录包插件。

**核心 Runtime 不出现任何 `if domain == ...`**（计划第 371 行）：它只通过
`DomainBase` 接口访问本类。想加一个领域 = 加一个同样的目录包 + 注册一行。

## 本领域的输入边界（**有意为之，不是缺陷**）

认这三类行结构：**syslog**（`Mon DD HH:MM:SS host proc[pid]: msg`，年份/时区可缺）、
**log4j**（`YYYY-MM-DD HH:MM:SS,mmm - LEVEL [thread] - msg`）、
**Apache error**（`[Day Mon DD HH:MM:SS YYYY] [level] msg`）。
指标行另认 `key=value` 形态（见 `parser.py`）。

**不认 Web access 日志**（`93.180.71.3 - - [17/May/2015:08:05:32 +0000] "GET /x" 304 0`）——
它是「站点访问」语义，不属于「主机/进程监控」这个领域：硬塞进来会让
analyzer 与 runbook 的判据都失去含义。这类文件上传后**整批计入坏行并附样本**
（计划第 540 行：无法解析的时间戳计坏行，绝不静默用当前时间替代），
负样本就在 `logs/nginx_plain.log`，量化结果见 `logs/README.md`。

要让平文 access 日志可解析 = **扩一个领域**（新目录包 + 注册一行），
不是放宽本领域的 parser —— 那会把两种语义混在同一套判据里。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.domains.computer_monitoring import config
from app.domains.computer_monitoring.analyzers import (
    DEFAULT_ANALYZERS,
    Analyzer,
)
from app.domains.computer_monitoring.knowledge import load_all
from app.domains.computer_monitoring.parser import (
    extract_metrics,
    infer_severity,
    parse_line,
)
from app.domains.computer_monitoring.runbooks import load_runbooks
from app.domains.protocol import (
    DomainBase,
    MetricSpec,
    NormalizedEvent,
    RawRecord,
    Runbook,
)

#: 本领域识别的指标。`source_key` 指 payload 里承载数值的键。
_METRIC_SPECS = (
    MetricSpec(metric_name="cpu_used", unit="%", source_key="value"),
    MetricSpec(metric_name="memory_used", unit="%", source_key="value"),
    MetricSpec(metric_name="disk_used", unit="%", source_key="value"),
    MetricSpec(metric_name="load_avg", unit="", source_key="value"),
)


class ComputerMonitoringDomain(DomainBase):
    """计算机监控领域包。"""

    domain_id = config.DOMAIN_ID
    version = config.DOMAIN_VERSION

    def __init__(self, package_dir: Path | None = None) -> None:
        self._dir = package_dir or Path(__file__).resolve().parent
        self._runbooks: list[Runbook] | None = None

    # ---------- 解析与归一化 ----------

    def parse_line(self, line: str) -> RawRecord | None:
        return parse_line(line)

    def normalize(self, raw: RawRecord) -> NormalizedEvent:
        """原始字段 → 标准事件。

        普通事件走 message；指标事件走 payload（计划第 348–349 行：指标不单独建表）。
        缺 timestamp 视为上游 bug，直接报错而**不用 now() 兜底**——
        静默补时间会让时间线分析建立在假数据上（红线 4）。
        """
        ts = raw.get("ts")
        if ts is None:
            raise ValueError(f"原始字段缺少 ts，无法归一化：{raw!r}")

        message = str(raw.get("msg", ""))
        payload = self._extract_metric_payload(raw)

        if payload is not None:
            return NormalizedEvent(
                timestamp=ts,
                message=message or f"{payload['metric_name']}={payload['value']}",
                event_type="metric",
                severity=infer_severity(raw),
                payload=payload,
                raw=dict(raw),
            )

        return NormalizedEvent(
            timestamp=ts,
            message=message,
            event_type="log",
            severity=infer_severity(raw),
            payload=None,
            raw=dict(raw),
        )

    def _extract_metric_payload(self, raw: RawRecord) -> dict[str, Any] | None:
        """从原始字段或消息文本里抽取指标。抽不到就返回 None（不硬凑成指标）。

        两条来源：
        1. 原始字段已带 `metric_name` / `value`（结构化 JSONL 日志走这条）；
        2. 消息文本里写了 `name=value`（真实系统日志走这条，见 `extract_metrics`）。

        缺了第 2 条，`CPUSpike` / `MemoryGrowth` / `DiskFull` 在真实日志上
        永远不会触发。
        """
        metric_name = raw.get("metric_name")
        value = raw.get("value")

        if metric_name is None or not isinstance(value, (int, float)):
            # 回退到从 message 抽
            extracted = extract_metrics(str(raw.get("msg", "")))
            if not extracted:
                return None
            primary = extracted[0]
            metric_name, value = primary["metric_name"], primary["value"]
            raw.setdefault("unit", primary["unit"])
            # 一条消息里有多个指标时，其余记进 raw 的 metrics 列表，避免丢失
            if len(extracted) > 1:
                raw["metrics"] = extracted

        spec = next((s for s in _METRIC_SPECS if s.metric_name == metric_name), None)
        return {
            "metric_name": str(metric_name),
            "value": float(value),
            "unit": spec.unit if spec is not None else str(raw.get("unit", "")),
        }

    # ---------- 契约的其余部分 ----------

    def metric_specs(self) -> list[MetricSpec]:
        return list(_METRIC_SPECS)

    def analyzers(self) -> list[Analyzer]:
        return list(DEFAULT_ANALYZERS)

    def runbooks(self) -> list[Runbook]:
        """懒加载并缓存 runbook（YAML 只在首次访问时读盘）。"""
        if self._runbooks is None:
            self._runbooks = load_runbooks(self._dir / "runbooks")
        return list(self._runbooks)

    def knowledge(self, data_dir: str | Path | None = None) -> dict[str, Any]:
        """加载领域知识：seed confirmed 叠加运行时 confirmed + staging。"""
        return load_all(
            self._dir,
            data_dir=Path(data_dir) if data_dir else None,
            domain_id=self.domain_id,
        )

    # ---------- runbook 命中 ----------

    def find_runbook_for(
        self,
        analyzer_name: str,
        message: str = "",
        metric_name: str | None = None,
        *,
        matched_pattern: str | None = None,
    ) -> Runbook | None:
        """按 analyzer 命中对应 runbook（计划第 430 行验收项）。

        匹配顺序：先看 `applies.analyzer`；若有 `message_pattern` 要求同时命中；
        有 `metric_name` 也要求一致。多候选时取 id 最小者，保证**确定性**。

        `matched_pattern` 是 analyzer **实际命中的那个关键词**（放在
        `anomaly["detail"]["pattern"]` 里）。为什么要单独传它：命中可能来自
        **进程名**而不是消息正文（`CrashReporterSupportHelper` 就是典型），
        此时消息里根本没有那个词，只按 `message` 匹配会漏掉本该命中的 runbook ——
        真机核验（2026-09-24）就是这么一条 runbook 都没挂上的：
        analyzer 命中 `crash`，而 `rb_crash_001` 要求 segfault/panic/core dumped。
        """
        matches: list[Runbook] = []
        for book in self.runbooks():
            applies = book.applies
            if applies.get("analyzer") != analyzer_name:
                continue
            expected_metric = applies.get("metric_name")
            if expected_metric is not None and expected_metric != metric_name:
                continue
            pattern = applies.get("message_pattern")
            if pattern is not None:
                haystack = f"{matched_pattern or ''} {message}"
                if not re.search(str(pattern), haystack, re.IGNORECASE):
                    continue
            matches.append(book)

        if not matches:
            return None
        return min(matches, key=lambda b: b.id)

    def knowledge_dir(self) -> Path:
        return self._dir
