"""Project —— **上下文边界**。

计划第 347 行：所有业务表都带 `project_id` 并建索引。
所有读写必须经由 repositories 层强制注入 project_id 过滤（计划第 362–363 行）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampTZ, utcnow


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # status 取值集合计划未定义，故只落 VARCHAR，不加 CheckConstraint
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    # 预算：货币单位统一为人民币元
    budget_total: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    budget_used: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:
        return f"<Project id={self.id} name={self.name!r}>"
