"""Evidence —— 支撑某条 Insight 的证据。

`event_ids` 里的每个 id 都必须属于**本次 Run 的有效事件集合**，
否则该条 Insight 不得标 `fact`（红线 3）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Evidence(Base):
    __tablename__ = "evidences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    insight_id: Mapped[int] = mapped_column(
        ForeignKey("insights.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 有效 event_id 列表
    event_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # 时间范围，如 {"start": "...", "end": "..."}
    time_range: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # 计算过程：让结论可复核，而不是「模型说的」
    calculation: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    def __repr__(self) -> str:
        return f"<Evidence id={self.id} insight_id={self.insight_id}>"
