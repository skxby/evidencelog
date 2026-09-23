"""Project 仓库。

Project 本身即边界，故不按 project_id 过滤 —— 它按 user_id 过滤（谁的账号）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_user(self, user_id: int) -> list[Project]:
        return list(
            self.session.execute(
                select(Project).where(Project.user_id == user_id).order_by(Project.id)
            ).scalars().all()
        )

    def get_for_user(self, user_id: int, project_id: int) -> Project | None:
        """按 (user_id, project_id) 取，防越权访问他人项目。"""
        return self.session.execute(
            select(Project).where(Project.id == project_id, Project.user_id == user_id)
        ).scalar_one_or_none()

    def add(self, project: Project) -> Project:
        self.session.add(project)
        return project
