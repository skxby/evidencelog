"""把 Pipeline 产出落库（计划第 751 行：Insight 落库）。

**红线 3 在落库这一侧同样成立**：`Evidence.event_ids` 必须 ⊆ 本次有效
`event_id`。上行（`evidence.py`）已经校验过一遍，这里在做写入前**再断言一次**——
因为这是最后一道闸门，一旦脏数据进库，报告页面就再也分不清"有证据"和"没证据"了。

Project 隔离经 repository：`Insight` 带 `project_id`，`Evidence` 通过
`insight_id` 归属（它没有 `project_id` 列，见阶段 02 的表结构）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.analysis.evidence import ValidatedInsight
from app.models.enums import INSIGHT_TYPE_FACT
from app.models.evidence import Evidence
from app.models.insight import Insight


class EvidencePersistenceError(ValueError):
    """落库前的最后一道证据校验没过 —— 拒绝写入，不静默放过。"""


@dataclass
class PersistenceResult:
    """落库结果。"""

    insight_ids: list[int] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    #: 因证据越界被拒绝的（不应发生；发生即为上游漏检）
    rejected: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "insight_count": len(self.insight_ids),
            "evidence_count": len(self.evidence_ids),
            "rejected": list(self.rejected),
        }


def persist_insights(
    *,
    project_id: int,
    run_id: int,
    insights: list[ValidatedInsight],
    valid_event_ids: set[str] | list[str],
    insight_repository: Any,
    evidence_repository: Any,
    source_id: int | None = None,
    incident_id_by_title: dict[str, int] | None = None,
) -> PersistenceResult:
    """把校验后的 Insight 与其 Evidence 写入数据库。

    `valid_event_ids` 是本次分析的有效集合；任何越界的 evidence 都会被拒绝
    该条 Insight（而不是悄悄裁掉写进去）——到了这一层还越界，说明上游有 bug，
    应当让它在测试里暴露，而不是被"容错"掩盖。
    """
    valid = {str(v) for v in valid_event_ids}
    result = PersistenceResult()
    incident_map = incident_id_by_title or {}

    for item in insights:
        evidence_ids = [str(e) for e in item.evidence_ids]
        invalid = [e for e in evidence_ids if e not in valid]

        # 落库前的最后一道闸门
        if invalid:
            result.rejected.append(
                f"{item.title}：evidence_ids 越界 {invalid}（上游校验漏检）"
            )
            continue
        if item.type == INSIGHT_TYPE_FACT and not evidence_ids:
            result.rejected.append(f"{item.title}：fact 缺少有效证据（上游校验漏检）")
            continue

        insight = Insight(
            project_id=project_id,
            run_id=run_id,
            incident_id=incident_map.get(item.title),
            type=item.type,
            severity=item.severity,
            confidence=item.confidence,
            title=item.title,
            summary=item.summary,
            reasoning=item.reasoning,
            limitations=item.limitations,
            # extras 只放 JSON 安全的确定性诊断信息（如 runbook 快照、命中的 analyzer）
            run_metadata=dict(item.extras) if item.extras else None,
        )
        insight_repository.add(project_id, insight)
        # 需要 id 才能挂 Evidence，故先 flush 拿到自增 id
        insight_repository.session.flush()
        result.insight_ids.append(int(insight.id))

        if evidence_ids:
            evidence = Evidence(
                insight_id=insight.id,
                source_id=source_id,
                event_ids=evidence_ids,
                time_range=None,
                calculation=None,
                description=f"支撑「{item.title}」的事件",
            )
            evidence_repository.add(project_id, evidence)
            evidence_repository.session.flush()
            result.evidence_ids.append(int(evidence.id))

    return result


def assert_no_evidence_violation(result: PersistenceResult) -> None:
    """把"上游漏检"变成显式失败。

    测试与生产路径都调它：如果被拒绝的条数不为 0，说明红线 3 在上游被绕过，
    必须让这件事响亮地暴露，而不是留在返回值的角落里（红线 4）。
    """
    if result.rejected:
        raise EvidencePersistenceError(
            "落库前证据校验失败：" + "；".join(result.rejected)
        )
