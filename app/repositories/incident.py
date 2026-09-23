"""Incident 仓库。

`group_ids` 是事故**真源**；本表不存冗余 `event_ids`（修订说明第 4 条）。
成员事件由成员分组派生：先拿 group_ids，再 `WHERE group_id IN (...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.incident import Incident
from app.repositories.base import ProjectScopedRepository


class IncidentRepository(ProjectScopedRepository[Incident]):
    model = Incident

    def create_incident(
        self,
        project_id: int,
        *,
        title: str,
        severity: str,
        group_ids: list[Any],
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        source_run_id: int | None = None,
        root_cause: str | None = None,
    ) -> Incident:
        return self.add(
            project_id,
            Incident(
                project_id=project_id,
                title=title,
                severity=severity,
                group_ids=list(group_ids),
                time_start=time_start,
                time_end=time_end,
                source_run_id=source_run_id,
                root_cause=root_cause,
            ),
        )

    def group_ids_of(self, project_id: int, incident_id: int) -> list[Any]:
        incident = self.get(project_id, incident_id)
        return list(incident.group_ids or []) if incident is not None else []

    def event_ids_of(self, project_id: int, incident_id: int) -> list[int]:
        """派生成员事件 id：group_ids → events.group_id。

        这就是「不存冗余 event_ids」的代价与做法——按需连表，而不是双写。
        """
        from sqlalchemy import select

        from app.models.event import Event

        group_ids = self.group_ids_of(project_id, incident_id)
        if not group_ids:
            return []
        rows = self.session.execute(
            select(Event.id)
            .where(Event.project_id == project_id, Event.group_id.in_(group_ids))
            .order_by(Event.id)
        ).scalars().all()
        return [int(r) for r in rows]
