"""复杂度评估：决定走 L0–L3（计划第 771–780 行）。

```text
L0：无 error 且无 anomaly            → 纯代码统计报告（与事件总量无关，
                                      量大只影响采样，不应因「正常但量大」升到 L3）
L1：error_count < 10 且无 high 异常   → 小模型归类解释
L2：error_count < 50，或异常涉及多个时间簇/类型
L3：存在 high severity 异常，或错误跨多簇多类型，或 L2 分析后置信度不足触发升级
事件总量只决定采样密度与是否提示缩小范围，不单独决定模型等级。
```

**最容易被写错的一条**：事件总量不决定等级。1 万条正常日志仍是 L0——
按总量升级会让"正常但量大"的日志白花钱，也会让 L0 那条"纯代码报告"永远走不到。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.gateways.base import TIER_L0, TIER_L1, TIER_L2, TIER_L3

#: 计划第 776–778 行的两个阈值起点（靠 Golden Set 迭代）
L1_MAX_ERRORS = 10
L2_MAX_ERRORS = 50

#: 事件数超过这个量级就提示缩小范围（只影响提示与采样密度，不影响等级）
LARGE_VOLUME_HINT = 10_000

#: 判定"多个时间簇"的窗口宽度与簇数门槛
CLUSTER_WINDOW_SECONDS = 300
MULTI_CLUSTER_THRESHOLD = 2

#: L2 分析后置信度低于此值触发升级到 L3（计划第 778 行）
LOW_CONFIDENCE_ESCALATION = 0.5


@dataclass
class ComplexityAssessment:
    """复杂度评估结果。"""

    tier: str
    error_count: int
    high_severity_count: int
    anomaly_count: int
    time_cluster_count: int
    event_type_count: int
    reasons: list[str] = field(default_factory=list)
    #: 只影响采样密度与提示，不决定等级
    volume_hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "error_count": self.error_count,
            "high_severity_count": self.high_severity_count,
            "anomaly_count": self.anomaly_count,
            "time_cluster_count": self.time_cluster_count,
            "event_type_count": self.event_type_count,
            "reasons": list(self.reasons),
            "volume_hint": self.volume_hint,
        }


def count_time_clusters(
    timestamps: list[Any], *, window_seconds: int = CLUSTER_WINDOW_SECONDS
) -> int:
    """把时间戳按 `window_seconds` 分桶，返回非空桶数。

    这就是计划第 767 行说的「文件内统计」（V1 不做跨文件/长期 Baseline）。
    无时间戳或非 datetime 的项被跳过——它们无法参与时间簇判定。
    """
    from datetime import datetime

    moments = sorted(
        ts for ts in timestamps if isinstance(ts, datetime)
    )
    if not moments:
        return 0

    clusters = 1
    window_start = moments[0]
    for moment in moments[1:]:
        if moment - window_start >= timedelta(seconds=window_seconds):
            clusters += 1
            window_start = moment
    return clusters


def assess_complexity(
    *,
    total_events: int,
    error_count: int,
    high_severity_count: int,
    anomaly_count: int,
    time_cluster_count: int,
    event_type_count: int,
    anomaly_types: int = 0,
    l2_confidence: float | None = None,
) -> ComplexityAssessment:
    """按计划第 774–779 行判定等级。

    `l2_confidence` 仅在"已经跑过 L2 想判断要不要升 L3"时传入（计划第 778 行）。
    """
    reasons: list[str] = []
    volume_hint = (
        f"事件量 {total_events} 较大，已提高采样密度；如结论不够具体可缩小时间范围"
        if total_events >= LARGE_VOLUME_HINT
        else None
    )

    # ---- L0：无 error 且无 anomaly ----
    if error_count == 0 and anomaly_count == 0:
        reasons.append("无 error 且无 anomaly → 纯代码统计报告")
        return ComplexityAssessment(
            tier=TIER_L0,
            error_count=error_count,
            high_severity_count=high_severity_count,
            anomaly_count=anomaly_count,
            time_cluster_count=time_cluster_count,
            event_type_count=event_type_count,
            reasons=reasons,
            volume_hint=volume_hint,
        )

    # ---- L3：存在 high severity 异常 / 错误跨多簇多类型 / L2 置信度不足 ----
    if high_severity_count > 0:
        reasons.append(f"存在 {high_severity_count} 个 high severity 异常 → L3")
        return _l3(
            reasons, volume_hint, error_count, high_severity_count, anomaly_count,
            time_cluster_count, event_type_count,
        )

    # 「L2 分析后置信度不足触发升级」是**独立**的升级条件，不受"是否多簇多类型"
    # 限制 —— 计划把它和另外两条并列为 L3 的触发原因。若把它写在
    # `multi_cluster and multi_type` 分支内部，就只有多簇多类型的场景才会升级，
    # 单簇低置信度的情况会被漏掉。
    if l2_confidence is not None and l2_confidence < LOW_CONFIDENCE_ESCALATION:
        reasons.append(
            f"L2 分析置信度 {l2_confidence} 低于 {LOW_CONFIDENCE_ESCALATION} → 升级 L3"
        )
        return _l3(
            reasons, volume_hint, error_count, high_severity_count, anomaly_count,
            time_cluster_count, event_type_count,
        )

    multi_cluster = time_cluster_count >= MULTI_CLUSTER_THRESHOLD
    multi_type = anomaly_types >= MULTI_CLUSTER_THRESHOLD
    if multi_cluster and multi_type:
        reasons.append(
            f"错误跨 {time_cluster_count} 个时间簇且涉及 {anomaly_types} 种异常类型 → L3"
        )
        return _l3(
            reasons, volume_hint, error_count, high_severity_count, anomaly_count,
            time_cluster_count, event_type_count,
        )

    # 多簇 / 多类型是 L2 的**上位**条件：即使错误数很少（< 10），只要异常跨
    # 多个时间簇或多类型，就该走 L2。计划把「异常涉及多个时间簇/类型」列为
    # L2 的定义，而 L1 描述的是「少量、单一」的错误。
    # L1 的判定顺序必须在 L2 之前（5 个错误不能被 error_count < 50 吞成 L2），
    # 但 L1 自身要排除掉这个上位条件。
    multi_cluster = time_cluster_count >= MULTI_CLUSTER_THRESHOLD
    multi_type = anomaly_types >= MULTI_CLUSTER_THRESHOLD
    multi_dimension = multi_cluster or multi_type

    # ---- L1：error_count < 10 且无 high 异常，且异常不跨多簇/多类型 ----
    if error_count < L1_MAX_ERRORS and not multi_dimension:
        reasons.append(f"error_count={error_count} < {L1_MAX_ERRORS} 且无 high 异常 → L1")
        return ComplexityAssessment(
            tier=TIER_L1,
            error_count=error_count,
            high_severity_count=high_severity_count,
            anomaly_count=anomaly_count,
            time_cluster_count=time_cluster_count,
            event_type_count=event_type_count,
            reasons=reasons,
            volume_hint=volume_hint,
        )

    # ---- L2：error_count < 50，或异常涉及多个时间簇/类型 ----
    if error_count < L2_MAX_ERRORS or multi_dimension:
        if multi_dimension:
            reasons.append(
                f"异常涉及 {time_cluster_count} 个时间簇 / {anomaly_types} 种类型 → L2"
            )
        else:
            reasons.append(f"error_count={error_count} < {L2_MAX_ERRORS} → L2")
        return ComplexityAssessment(
            tier=TIER_L2,
            error_count=error_count,
            high_severity_count=high_severity_count,
            anomaly_count=anomaly_count,
            time_cluster_count=time_cluster_count,
            event_type_count=event_type_count,
            reasons=reasons,
            volume_hint=volume_hint,
        )

    # 兜底：错误 ≥ 50 但既不 high 也不多簇多类型 —— 归 L2（比 L1 保守）
    reasons.append(f"error_count={error_count} ≥ {L2_MAX_ERRORS} 但无 high 异常 → 归 L2")
    return ComplexityAssessment(
        tier=TIER_L2,
        error_count=error_count,
        high_severity_count=high_severity_count,
        anomaly_count=anomaly_count,
        time_cluster_count=time_cluster_count,
        event_type_count=event_type_count,
        reasons=reasons,
        volume_hint=volume_hint,
    )


def _l3(
    reasons: list[str],
    volume_hint: str | None,
    error_count: int,
    high_severity_count: int,
    anomaly_count: int,
    time_cluster_count: int,
    event_type_count: int,
) -> ComplexityAssessment:
    return ComplexityAssessment(
        tier=TIER_L3,
        error_count=error_count,
        high_severity_count=high_severity_count,
        anomaly_count=anomaly_count,
        time_cluster_count=time_cluster_count,
        event_type_count=event_type_count,
        reasons=reasons,
        volume_hint=volume_hint,
    )


def should_escalate_after_l2(l2_confidence: float) -> bool:
    """L2 跑完判断要不要升 L3（计划第 778 行）。"""
    return l2_confidence < LOW_CONFIDENCE_ESCALATION
