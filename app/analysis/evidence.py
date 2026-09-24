"""Evidence 校验与幻觉拦截（红线 3，计划第 838–848 行）。

```text
1. 解析/蒸馏阶段收集本次有效 event_id → valid_ids
2. 模型返回后检查 evidence_ids ⊆ valid_ids
3. 处理：
   无效占比 > 50%      → 拒绝该 Insight，带反馈重试（最多 3 次）
   无效占比 ≤ 50%      → 移除无效 id，按比例下调 confidence
   全部无效但 possibility → 保留并在 limitations 注明
   重试仍失败          → fact 一律降级为 possibility，写明「模型未能提供有效证据」
```

**这是"没有有效 Evidence 的结论不能标 fact"的实现位置。** 模型编造 event_id
是本项目最危险的失败模式：报告看起来完全正常，但结论是凭空来的。

判定顺序在实现上很重要：先算无效占比，再决定是"拒绝重试"还是"裁剪降级"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.enums import (
    INSIGHT_TYPE_FACT,
    INSIGHT_TYPE_INFERENCE,
    INSIGHT_TYPE_POSSIBILITY,
    INSIGHT_TYPE_UNKNOWN,
    INSIGHT_TYPES,
)

#: 计划第 844 行：无效占比超过这个比例就整条拒绝并重试
REJECT_INVALID_RATIO = 0.50

#: 计划第 844 行：最多 3 次
MAX_EVIDENCE_RETRIES = 3

#: 合法类型直接取自 `app.models.enums` —— **不要再抄一份**。
#: 抄成两份的后果实测过：这里认 4 种、数据库约束只认 3 种，
#: 于是模型按 schema 给出的 `unknown` 结论在落库时才被 CHECK 拒掉，
#: 整次分析以"任务崩溃 + Run 停在 queued"收场。
VALID_INSIGHT_TYPES = INSIGHT_TYPES

#: 该类型必须有 evidence（计划第 833 行）
TYPES_REQUIRING_EVIDENCE = (INSIGHT_TYPE_FACT,)
#: 该类型必须有 reasoning（计划第 834 行）
TYPES_REQUIRING_REASONING = (INSIGHT_TYPE_INFERENCE,)
#: 该类型必须有 limitations（计划第 835–836 行）
TYPES_REQUIRING_LIMITATIONS = (INSIGHT_TYPE_POSSIBILITY, INSIGHT_TYPE_UNKNOWN)


class EvidenceViolationError(ValueError):
    """Insight 结构本身不合法（缺必填字段）。"""


@dataclass
class ValidatedInsight:
    """校验后的 Insight。所有字段都已按规则补全或降级。"""

    type: str
    severity: str
    confidence: float
    title: str
    summary: str
    evidence_ids: list[str] = field(default_factory=list)
    reasoning: str | None = None
    limitations: str | None = None
    #: 被移除的无效 id（留痕，便于排查模型在编什么）
    removed_evidence_ids: list[str] = field(default_factory=list)
    #: 本条的处置说明（降级 / 裁剪原因）
    notes: list[str] = field(default_factory=list)
    #: 附带的确定性诊断信息（命中的 analyzer、对应 runbook 等）。
    #: 与模型输出无关，由流水线填入，落库到 `Insight.run_metadata`。
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def has_valid_evidence(self) -> bool:
        return bool(self.evidence_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "confidence": round(self.confidence, 4),
            "title": self.title,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
            "reasoning": self.reasoning,
            "limitations": self.limitations,
            "removed_evidence_ids": list(self.removed_evidence_ids),
            "notes": list(self.notes),
            "extras": dict(self.extras),
        }


@dataclass
class ValidationOutcome:
    """一批 Insight 的校验结果。"""

    accepted: list[ValidatedInsight] = field(default_factory=list)
    #: 需要带反馈重试的（无效占比 > 50%）
    rejected: list[dict[str, Any]] = field(default_factory=list)
    #: 给模型的重试反馈
    feedback: list[str] = field(default_factory=list)

    @property
    def needs_retry(self) -> bool:
        return bool(self.rejected)


def normalize_evidence_ids(raw: Any) -> list[str]:
    """把模型给的 evidence_ids 规范化成字符串列表。

    模型可能给 int、str 或混合；统一成 str 再比对，避免 `1` 与 `"1"` 被当成
    两个不同的 id（那会让明明有效的证据被判为无效）。
    """
    if raw is None:
        return []
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def classify_evidence(
    evidence_ids: list[str], valid_ids: set[str]
) -> tuple[list[str], list[str], float]:
    """返回 (有效 id, 无效 id, 无效占比)。"""
    if not evidence_ids:
        return [], [], 0.0
    valid = [e for e in evidence_ids if e in valid_ids]
    invalid = [e for e in evidence_ids if e not in valid_ids]
    return valid, invalid, len(invalid) / len(evidence_ids)


def validate_insight(
    raw: dict[str, Any],
    *,
    valid_ids: set[str],
) -> tuple[ValidatedInsight | None, str | None]:
    """校验单条 Insight。

    返回 `(校验后的 Insight, 拒绝原因)`。拒绝原因非空表示应带反馈重试。
    类型 / 必填字段的**结构性**问题直接抛 `EvidenceViolationError`——
    那是模型的输出格式错，属于不可重试的校验失败，重试同一提示通常还是错。
    """
    if not isinstance(raw, dict):
        raise EvidenceViolationError(f"Insight 必须是对象，收到 {type(raw).__name__}")

    insight_type = str(raw.get("type", "")).strip().lower()
    if insight_type not in VALID_INSIGHT_TYPES:
        raise EvidenceViolationError(
            f"type={insight_type!r} 非法，只接受 {VALID_INSIGHT_TYPES}"
        )

    title = str(raw.get("title", "")).strip()
    summary = str(raw.get("summary", "")).strip()
    if not title:
        raise EvidenceViolationError("Insight 缺少 title")

    severity = str(raw.get("severity", "low")).strip().lower()
    if severity not in ("high", "medium", "low"):
        severity = "low"

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    evidence_ids = normalize_evidence_ids(raw.get("evidence_ids"))
    valid, invalid, invalid_ratio = classify_evidence(evidence_ids, valid_ids)

    notes: list[str] = []

    # ---- 计划第 844 行：无效占比 > 50% → 拒绝，带反馈重试 ----
    if evidence_ids and invalid_ratio > REJECT_INVALID_RATIO:
        return None, (
            f"「{title}」引用的 event_id 有 {len(invalid)}/{len(evidence_ids)} 不存在"
            f"（占比 {invalid_ratio:.0%} > 50%）。请只引用本次有效事件里真实存在的 "
            f"event_id，或改用 possibility 并说明为何无法确认。"
        )

    if invalid:
        # ---- 计划第 845 行：≤ 50% → 移除无效 id，按比例下调 confidence ----
        kept_ratio = len(valid) / len(evidence_ids)
        confidence = round(confidence * kept_ratio, 4)
        notes.append(
            f"移除了 {len(invalid)} 个无效 event_id，confidence 按有效比例"
            f"下调至 {confidence}"
        )

    reasoning = _optional_text(raw.get("reasoning"))
    limitations = _optional_text(raw.get("limitations"))

    # ---- 计划第 846 行：全部无效但 possibility → 保留并注明 ----
    if evidence_ids and not valid and insight_type == INSIGHT_TYPE_POSSIBILITY:
        limitations = _append(
            limitations,
            "模型提供的证据全部无效，本条仅作可能原因，未能确认",
        )
        notes.append("证据全部无效，保留为 possibility")

    # ---- 红线 3：没有有效证据的 fact 不能是 fact ----
    if insight_type == INSIGHT_TYPE_FACT and not valid:
        insight_type = INSIGHT_TYPE_POSSIBILITY
        limitations = _append(
            limitations,
            "模型未能提供有效证据，fact 已降级为 possibility",
        )
        notes.append("无有效证据，fact 降级为 possibility")

    # ---- 必填字段强制（计划第 833–836 行）----
    if insight_type in TYPES_REQUIRING_REASONING and not reasoning:
        reasoning = "（模型未给出推理过程）"
        notes.append("inference 缺 reasoning，已补占位说明")

    if insight_type in TYPES_REQUIRING_LIMITATIONS and not limitations:
        limitations = "（模型未说明局限）"
        notes.append("缺 limitations，已补占位说明")

    return (
        ValidatedInsight(
            type=insight_type,
            severity=severity,
            confidence=confidence,
            title=title,
            summary=summary,
            evidence_ids=valid,
            reasoning=reasoning,
            limitations=limitations,
            removed_evidence_ids=invalid,
            notes=notes,
        ),
        None,
    )


def validate_insights(
    raw_insights: Any, *, valid_ids: set[str]
) -> ValidationOutcome:
    """校验一批 Insight，汇总可接受项与需重试项。"""
    outcome = ValidationOutcome()
    if not raw_insights:
        return outcome
    if not isinstance(raw_insights, (list, tuple)):
        raise EvidenceViolationError(
            f"insights 必须是数组，收到 {type(raw_insights).__name__}"
        )

    for raw in raw_insights:
        validated, reason = validate_insight(raw, valid_ids=valid_ids)
        if validated is not None:
            outcome.accepted.append(validated)
        else:
            outcome.rejected.append(raw if isinstance(raw, dict) else {"raw": raw})
            if reason:
                outcome.feedback.append(reason)
    return outcome


def build_retry_feedback(outcome: ValidationOutcome) -> str:
    """把拒绝原因拼成给模型的反馈（计划第 844 行「带反馈重试」）。"""
    if not outcome.feedback:
        return ""
    lines = ["你上一次的输出存在证据问题，请修正后重新输出："]
    lines.extend(f"- {item}" for item in outcome.feedback)
    lines.append("只允许引用上面给出的有效 event_id。")
    return "\n".join(lines)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _append(existing: str | None, extra: str) -> str:
    if not existing:
        return extra
    if extra in existing:
        return existing
    return f"{existing}；{extra}"
