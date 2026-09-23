"""Event 仓库。

验收要点（计划第 365 行）：能插入普通事件与指标事件并按 project 查回。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models import enums
from app.models.event import Event
from app.repositories.base import ProjectScopedRepository


class EventRepository(ProjectScopedRepository[Event]):
    model = Event

    def list_metric_events(self, project_id: int) -> list[Event]:
        """指标事件：`event_type="metric"`，数值在 payload 里（指标不单独建表）。"""
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.event_type == enums.EVENT_TYPE_METRIC)
                .order_by(Event.timestamp)
            ).scalars().all()
        )

    def list_log_events(self, project_id: int) -> list[Event]:
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.event_type == enums.EVENT_TYPE_LOG)
                .order_by(Event.timestamp)
            ).scalars().all()
        )

    def list_by_group(self, project_id: int, group_id: int) -> list[Event]:
        """按分组取成员事件。

        这正是「EventGroup 不存 event_ids 反向数组」后推荐的查法：
        `WHERE group_id = ?`（计划第 358 行）。
        """
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.group_id == group_id)
                .order_by(Event.timestamp)
            ).scalars().all()
        )

    def count_by_severity(self, project_id: int) -> dict[str, int]:
        """各 severity 分布（阶段 05 的 stats_calculator 会用到）。"""
        from sqlalchemy import func

        rows = self.session.execute(
            select(Event.severity, func.count())
            .where(Event.project_id == project_id)
            .group_by(Event.severity)
        ).all()
        return {severity: int(count) for severity, count in rows}

    def add_metric(
        self,
        project_id: int,
        *,
        source_id: int,
        timestamp: Any,
        metric_name: str,
        value: float,
        unit: str,
        severity: str = enums.SEVERITY_LOW,
    ) -> Event:
        """便捷构造指标事件，保证 payload 结构一致。"""
        return self.add(
            project_id,
            Event(
                project_id=project_id,
                source_id=source_id,
                timestamp=timestamp,
                event_type=enums.EVENT_TYPE_METRIC,
                severity=severity,
                message=f"{metric_name}={value}{unit}",
                payload={"metric_name": metric_name, "value": value, "unit": unit},
            ),
        )
