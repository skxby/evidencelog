"""EventGroup 仓库。

`event_count` 是分组时一次性写好的**缓存计数**，不随事件逐条更新
（修订说明第 4 条）。故意**不提供**「每条事件 +1」的方法，避免把缓存计数
用成实时计数器而失去意义。
"""

from __future__ import annotations

from datetime import datetime

from app.models.event_group import EventGroup
from app.repositories.base import ProjectScopedRepository


class EventGroupRepository(ProjectScopedRepository[EventGroup]):
    model = EventGroup

    def find_by_key(
        self, project_id: int, source_id: int, group_key: str
    ) -> EventGroup | None:
        """按 (project, source, group_key) 取——与唯一约束同口径。"""
        return self.session.execute(
            self.scoped(project_id).where(
                EventGroup.source_id == source_id,
                EventGroup.group_key == group_key,
            )
        ).scalar_one_or_none()

    def create_group(
        self,
        project_id: int,
        *,
        source_id: int,
        group_key: str,
        template: str | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> EventGroup:
        return self.add(
            project_id,
            EventGroup(
                project_id=project_id,
                source_id=source_id,
                group_key=group_key,
                template=template,
                time_start=time_start,
                time_end=time_end,
                event_count=0,
            ),
        )

    def set_event_count(self, project_id: int, group_id: int, count: int) -> bool:
        """显式设置缓存计数（分组结束时调用一次，而非逐条累加）。"""
        group = self.get(project_id, group_id)
        if group is None:
            return False
        group.event_count = count
        return True
