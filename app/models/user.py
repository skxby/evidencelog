"""User —— 账号（V1 自用单账号，不做角色系统）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampTZ, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # 存 bcrypt 哈希。用 bcrypt 而非 passlib（AGENTS.md 禁止 passlib）。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
