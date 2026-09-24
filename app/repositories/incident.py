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

    def recent_for_context(self, project_id: int, *, limit: int = 5) -> list[dict]:
        """给 Context 用的**历史事故**（计划第 787 行：历史相似事故注入）。

        为什么必须由这里提供：`merge_into_incidents(existing_incidents=...)` 需要
        `source_id` / `signature` / 时间窗三项才能判断"这次的和历史的是不是同一类"，
        而 Incident 表只存了 title / severity / group_ids / 时间。
        `source_id` 由成员分组派生、`signature` 由标题前缀承载（见
        `app.analysis.persistence.incident_title` 的约定说明）——
        两处都是**派生**而不是新加列，冻结表不动。

        取最近 `limit` 条：Context 有预算，历史事故是背景信息，多了只会挤掉当前证据。
        """
        from sqlalchemy import select

        from app.models.event_group import EventGroup

        rows = list(
            self.session.execute(
                self.scoped(project_id)
                .order_by(Incident.time_end.desc().nullslast(), Incident.id.desc())
                .limit(limit)
            ).scalars().all()
        )
        if not rows:
            return []

        # 一条查询把所有成员分组的 source_id 取回来（避免逐条 incident 查一次）
        all_group_ids = {int(g) for r in rows for g in (r.group_ids or [])}
        source_by_group: dict[int, int] = {}
        if all_group_ids:
            pairs = self.session.execute(
                select(EventGroup.id, EventGroup.source_id).where(
                    EventGroup.project_id == project_id,
                    EventGroup.id.in_(sorted(all_group_ids)),
                )
            ).all()
            source_by_group = {int(gid): int(sid) for gid, sid in pairs}

        from app.analysis.persistence import signature_from_title

        out: list[dict] = []
        for row in rows:
            group_ids = [int(g) for g in (row.group_ids or [])]
            source_id = next(
                (source_by_group[g] for g in group_ids if g in source_by_group), None
            )
            out.append(
                {
                    "id": int(row.id),
                    "source_id": source_id,
                    "signature": signature_from_title(row.title),
                    "title": row.title,
                    "severity": row.severity,
                    "group_ids": group_ids,
                    "time_start": row.time_start,
                    "time_end": row.time_end,
                }
            )
        return out
