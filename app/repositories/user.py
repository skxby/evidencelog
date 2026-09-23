"""User 仓库。

User 不属于任何 project（登录前就要查），故**不继承** ProjectScopedRepository。
它的隔离边界是「单账号自用」，不是 Project。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_email(self, email: str) -> User | None:
        return self.session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def add(self, user: User) -> User:
        self.session.add(user)
        return user
