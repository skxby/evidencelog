"""SQLAlchemy 模型（9 张表）。

`Base.metadata` 是 Alembic autogenerate 的比对源 —— 见 migrations/env.py。
**导入顺序有讲究**：Event 与 Incident 互相引用，靠 `use_alter=True` 处理，
故两者都必须被导入，否则 metadata 里会缺表。
"""

from __future__ import annotations

from app.models.agent_run import AgentRun
from app.models.base import Base, TimestampTZ, utcnow
from app.models.datasource import DataSource
from app.models.event import Event
from app.models.event_group import EventGroup
from app.models.evidence import Evidence
from app.models.incident import Incident
from app.models.insight import Insight
from app.models.project import Project
from app.models.user import User

#: 冻结的 9 张业务表。任何第 10 张都是改对象边界 —— 必须先问（硬约束 6）。
MODELS = (
    User,
    Project,
    DataSource,
    Event,
    EventGroup,
    Incident,
    AgentRun,
    Insight,
    Evidence,
)

__all__ = [
    "MODELS",
    "AgentRun",
    "Base",
    "DataSource",
    "Event",
    "EventGroup",
    "Evidence",
    "Incident",
    "Insight",
    "Project",
    "TimestampTZ",
    "User",
    "utcnow",
]
