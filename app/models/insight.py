"""Insight —— 带证据的结论。

红线 3：**没有有效 Evidence 的结论不能标 `fact`**。
`evidence_ids` 必须 ⊆ 本次有效 `event_id`，越界即拦截/降级（阶段 09 实现校验）。

根因类结论只能标 `inference` / `possibility`（计划第 1112 行）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums
from app.models.base import Base, TimestampTZ, utcnow


class Insight(Base):
    __tablename__ = "insights"
    __table_args__ = (
        CheckConstraint(enums.insight_type_check_sql(), name="insight_type"),
        CheckConstraint(enums.severity_check_sql("insights.severity"), name="insight_severity"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="insight_confidence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 可空：有些 Insight 不归属于具体事故
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )

    type: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # 0.0–1.0
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 局限说明：降级 / 证据不足时必须写明，不许静默（红线 4）
    limitations: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )

    def __repr__(self) -> str:
        return f"<Insight id={self.id} type={self.type!r} conf={self.confidence}>"
