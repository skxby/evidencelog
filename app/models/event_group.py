"""EventGroup —— 同模板 / 同时间窗的事件归为一组（500 告警 → N 组）。

**真源 = `Event.group_id`**：本表**刻意不存** `event_ids` 反向数组（计划第 358 行、
修订说明第 4 条）。查成员用 `WHERE group_id = ?`。
`event_count` 是分组时一次性写好的缓存计数，不随事件逐条更新（修订说明第 4 条）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampTZ, utcnow


class EventGroup(Base):
    __tablename__ = "event_groups"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_id", "group_key", name="project_source_group_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 分组键：同模板 + 同时间窗的指纹
    group_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 模板：手写「正则变量替换」得到的占位符串（V1 不引 Drain3，修订说明第 6a 条）
    template: Mapped[str | None] = mapped_column(Text, nullable=True)

    time_start: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True, index=True)
    time_end: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    # 缓存计数，不随事件逐条更新
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # status 取值集合计划未定义，只落 VARCHAR
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )

    def __repr__(self) -> str:
        return f"<EventGroup id={self.id} group_key={self.group_key!r} count={self.event_count}>"
