"""Event —— 日志事件与指标事件的统一载体。

计划第 348–349 行：**指标不单独建表**，CPU/内存/磁盘数值以
`event_type="metric"` + `payload={"metric_name","value","unit"}` 写入本表。

`group_id` 是分组**唯一真源**（计划第 358 行）。
`incident_id` 与 `Incident.group_ids` 构成引用环 —— 该外键用 `use_alter=True`
延迟到建表后添加，避免建表顺序死锁（PostgreSQL 无「前向引用」）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums
from app.models.base import Base, TimestampTZ


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(enums.event_type_check_sql("events.event_type"), name="event_type"),
        CheckConstraint(enums.severity_check_sql("events.severity"), name="event_severity"),
        # 按 project 查事件是最热的路径（计划第 347 行要求 project_id 索引）
        Index("ix_events_project_id_timestamp", "project_id", "timestamp"),
    )

    # 单次分析可达 1 万事件量级，用 BigInteger 预留增长空间
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 分组归属的唯一真源；EventGroup 不存反向数组
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 与 Incident.group_ids 构成环，延迟添加约束
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "incidents.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_events_incident_id_incidents",
        ),
        nullable=True,
        index=True,
    )

    timestamp: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=enums.EVENT_TYPE_LOG
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=enums.SEVERITY_LOW
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 指标事件的载体，如 {"metric_name":"cpu_used","value":92.4,"unit":"%"}
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # 列名 metadata，属性名 meta（保留名规避）
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<Event id={self.id} type={self.event_type!r} sev={self.severity!r}>"
