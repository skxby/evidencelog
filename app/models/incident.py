"""Incident —— 一次事故，可含多个分组；跨 Run 检索的「事故记忆库」。

**真源 = `group_ids`**：本表**刻意不存**冗余 `event_ids`（计划第 358 行、
修订说明第 4 条）——成员事件可由成员分组派生。

归并是**确定性代码**（红线 1），V1 用「同源 + 同时间窗 + 同异常签名」近似，
明确**不是根因判定**（修订说明第 5 条）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums
from app.models.base import Base, TimestampTZ, utcnow


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            enums.severity_check_sql("incidents.severity"), name="incident_severity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # status 取值集合计划未定义，只落 VARCHAR
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    severity: Mapped[str] = mapped_column(String(16), nullable=False)

    time_start: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True, index=True)
    time_end: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    # 事故真源：成员分组 id 列表（V1 无中间表，计划第 358 行）
    group_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    # 根因由模型给出，只能标 inference / possibility 并挂证据（计划第 1112 行）
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:
        return f"<Incident id={self.id} title={self.title!r} sev={self.severity!r}>"
