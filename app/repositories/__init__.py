"""Repository 层：Project 隔离的唯一入口。

计划第 360–363 行要求「所有读写走统一的 repository 函数，内部强制带 project_id
条件」。业务代码不应直接 `session.execute(select(Model))`。
"""

from __future__ import annotations

from app.repositories.agent_run import AgentRunRepository
from app.repositories.base import ProjectScopedRepository
from app.repositories.datasource import DataSourceRepository
from app.repositories.event import EventRepository
from app.repositories.event_group import EventGroupRepository
from app.repositories.evidence import EvidenceRepository
from app.repositories.incident import IncidentRepository
from app.repositories.insight import InsightRepository
from app.repositories.project import ProjectRepository
from app.repositories.user import UserRepository

__all__ = [
    "AgentRunRepository",
    "DataSourceRepository",
    "EventGroupRepository",
    "EventRepository",
    "EvidenceRepository",
    "IncidentRepository",
    "InsightRepository",
    "ProjectRepository",
    "ProjectScopedRepository",
    "UserRepository",
]
