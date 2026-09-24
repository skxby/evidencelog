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
            )
            .scalars()
            .all()
        )

    def list_log_events(self, project_id: int) -> list[Event]:
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.event_type == enums.EVENT_TYPE_LOG)
                .order_by(Event.timestamp)
            )
            .scalars()
            .all()
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
            )
            .scalars()
            .all()
        )

    def pipeline_events(
        self, project_id: int, *, limit: int = 50_000
    ) -> list[dict[str, Any]]:
        """装载喂给分析链路的事件字典 —— **唯一**的 ORM→链路字段映射。

        为什么必须是唯一一处：这段映射很容易漏字段，而漏掉的后果是**静默的**。
        `metadata` 里的 `proc`（进程名）就是活例子 —— 少了它，
        `ProcessCrash` 看不到 `CrashReporterSupportHelper` 这类崩溃上报进程，
        候选异常为 0 → 复杂度评为 L0 → 走"纯规则报告"分支**根本不会调用模型**，
        最后以"分析完成、零结论、零花费"收场：每一步都"成功"，真正的原因
        （字段没装载）一个字都不剩。同一条映射抄在两个地方就必然会漂移，
        所以 Worker 与本函数共用它。
        """
        rows = self.session.execute(
            self.scoped(project_id).order_by(Event.timestamp, Event.id).limit(limit)
        ).scalars()
        return [
            {
                "event_id": int(row.id),
                "source_id": int(row.source_id),
                "timestamp": row.timestamp,
                "severity": row.severity,
                "event_type": row.event_type,
                "message": row.message,
                "payload": row.payload,
                # 列名 metadata、属性名 meta；analyzer 依赖其中的 proc/pid/host
                "metadata": dict(row.meta or {}),
            }
            for row in rows
        ]

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
