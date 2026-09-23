"""领域检测器（analyzer）。

边界（计划第 396–402 行）：
    tool     通用数据算子：过滤 / 统计 / 时间窗口，跨领域复用，**不含领域判断**
    analyzer 领域检测：含阈值与判断逻辑，是 domain 私有资产，产出候选异常

所以阈值属于这里，不属于 `app/tools/`。

**红线 1：全部是纯确定性代码，绝不调用 LLM。**
**红线 4：不静默** —— 样本不足时明确返回「未判定」（空列表），而不是假装通过。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from itertools import pairwise
from typing import Any

from app.domains.protocol import AnalyzerFinding

# ============================================================
# V1 阈值起点（计划第 407 行：阈值先保守，靠 Golden Set 调整）
# ============================================================

CPU_SPIKE_THRESHOLD = 90.0
CPU_SPIKE_MIN_SAMPLES = 3

MEMORY_GROWTH_THRESHOLD = 90.0
MEMORY_GROWTH_MIN_SAMPLES = 4
#: 窗口内内存增幅超过这个比例才算「持续增长」（保守起点）
MEMORY_GROWTH_MIN_RATIO = 0.10

DISK_FULL_THRESHOLD = 95.0

#: ProcessCrash 命中的模式（计划第 411 行：crash / segfault / oom 等）
CRASH_PATTERNS = (
    "segfault",
    "segmentation fault",
    "core dumped",
    "oom-killer",
    "out of memory",
    "killed process",
    "panic",
    "crash",
)


class Analyzer(ABC):
    """确定性检测器基类。

    `analyze` 接收指标样本或事件行（由具体 analyzer 决定）。
    """

    #: 与 runbook 的 `applies.analyzer` 对应
    name: str

    @abstractmethod
    def analyze(self, samples: list[dict[str, Any]]) -> list[AnalyzerFinding]:
        """产出候选异常。样本不足时返回空列表（是"不判定"，不是"通过"）。"""


def _metric_samples(samples: list[dict[str, Any]], metric_name: str) -> list[dict[str, Any]]:
    return [s for s in samples if s.get("metric_name") == metric_name]


def _values(rows: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = row.get("value")
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


class CPUSpike(Analyzer):
    """`cpu_used > 90%` 持续若干采样点（计划第 408 行）。"""

    name = "CPUSpike"

    def __init__(
        self,
        threshold: float = CPU_SPIKE_THRESHOLD,
        min_samples: int = CPU_SPIKE_MIN_SAMPLES,
    ) -> None:
        self.threshold = threshold
        self.min_samples = min_samples

    def analyze(self, samples: list[dict[str, Any]]) -> list[AnalyzerFinding]:
        values = _values(_metric_samples(samples, "cpu_used"))
        if len(values) < self.min_samples:
            return []  # 样本不足：不判定

        consecutive = 0
        peak = 0.0
        for value in values:
            if value > self.threshold:
                consecutive += 1
                peak = max(peak, value)
            else:
                consecutive = 0

        if consecutive < self.min_samples:
            return []

        return [
            AnalyzerFinding(
                analyzer=self.name,
                severity="high" if peak > 95 else "medium",
                message=(
                    f"CPU 持续高位：连续 {consecutive} 个采样点超过 {self.threshold}%，"
                    f"峰值 {peak}%"
                ),
                metric_name="cpu_used",
                detail={
                    "threshold": self.threshold,
                    "consecutive_samples": consecutive,
                    "peak": peak,
                },
            )
        ]


class MemoryGrowth(Analyzer):
    """内存指标在时间窗口内持续上升（计划第 409 行）。

    判据是「单调上升次数」与「首尾增幅」**两者都满足**——
    只看首尾会把「抖动后回落」误判成增长。
    """

    name = "MemoryGrowth"

    def __init__(
        self,
        min_samples: int = MEMORY_GROWTH_MIN_SAMPLES,
        min_ratio: float = MEMORY_GROWTH_MIN_RATIO,
        threshold: float = MEMORY_GROWTH_THRESHOLD,
    ) -> None:
        self.min_samples = min_samples
        self.min_ratio = min_ratio
        self.threshold = threshold

    def analyze(self, samples: list[dict[str, Any]]) -> list[AnalyzerFinding]:
        values = _values(_metric_samples(samples, "memory_used"))
        if len(values) < self.min_samples:
            return []

        rises = sum(1 for a, b in pairwise(values) if b > a)
        first, last = values[0], values[-1]
        if first <= 0:
            return []
        ratio = (last - first) / first

        if rises < self.min_samples - 1 or ratio < self.min_ratio:
            return []

        over_threshold = last > self.threshold
        headline = (
            f"内存持续增长且已超过 {self.threshold}%"
            if over_threshold
            else "内存持续增长"
        )
        return [
            AnalyzerFinding(
                analyzer=self.name,
                severity="high" if over_threshold else "medium",
                message=(
                    f"{headline}：窗口内 {rises} 次上升，"
                    f"由 {first:.1f} 增至 {last:.1f}（+{ratio * 100:.1f}%）"
                ),
                metric_name="memory_used",
                detail={
                    "rises": rises,
                    "first": first,
                    "last": last,
                    "ratio": ratio,
                    "min_ratio": self.min_ratio,
                },
            )
        ]


class DiskFull(Analyzer):
    """`disk_used > 95%`（计划第 410 行）。"""

    name = "DiskFull"

    def __init__(self, threshold: float = DISK_FULL_THRESHOLD) -> None:
        self.threshold = threshold

    def analyze(self, samples: list[dict[str, Any]]) -> list[AnalyzerFinding]:
        values = _values(_metric_samples(samples, "disk_used"))
        if not values:
            return []
        peak = max(values)
        if peak <= self.threshold:
            return []
        return [
            AnalyzerFinding(
                analyzer=self.name,
                severity="high",
                message=f"磁盘占用 {peak}% 超过阈值 {self.threshold}%",
                metric_name="disk_used",
                detail={"threshold": self.threshold, "peak": peak},
            )
        ]


class ProcessCrash(Analyzer):
    """message 命中 crash / segfault / oom 等模式（计划第 411 行）。

    只做确定性关键词匹配 —— **不是根因判定**，产出的是候选异常。
    """

    name = "ProcessCrash"

    def __init__(self, patterns: tuple[str, ...] = CRASH_PATTERNS) -> None:
        self.patterns = patterns

    def analyze(self, samples: list[dict[str, Any]]) -> list[AnalyzerFinding]:
        findings: list[AnalyzerFinding] = []
        for row in samples:
            message = str(row.get("message", ""))
            lowered = message.lower()
            hit = next((p for p in self.patterns if p in lowered), None)
            if hit is None:
                continue
            findings.append(
                AnalyzerFinding(
                    analyzer=self.name,
                    severity="high",
                    message=f"进程异常迹象（命中 “{hit}”）：{message[:200]}",
                    event_ids=[row["event_id"]] if "event_id" in row else [],
                    detail={"pattern": hit},
                )
            )
        return findings


#: 顺序即报告展示顺序
DEFAULT_ANALYZERS: tuple[Analyzer, ...] = (
    CPUSpike(),
    MemoryGrowth(),
    DiskFull(),
    ProcessCrash(),
)


def run_analyzers(
    samples: list[dict[str, Any]], analyzers: tuple[Analyzer, ...] = DEFAULT_ANALYZERS
) -> list[AnalyzerFinding]:
    """依次跑所有 analyzer，汇总候选异常。

    任一步失败都直接抛出——不吞异常、不返回「部分成功但看着正常」的结果（红线 4）。
    """
    findings: list[AnalyzerFinding] = []
    for analyzer in analyzers:
        findings.extend(analyzer.analyze(samples))
    return findings
