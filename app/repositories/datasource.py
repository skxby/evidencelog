"""DataSource 仓库。"""

from __future__ import annotations

from app.models.datasource import DataSource
from app.repositories.base import ProjectScopedRepository


class DataSourceRepository(ProjectScopedRepository[DataSource]):
    model = DataSource

    def find_by_format(self, project_id: int, fmt: str) -> list[DataSource]:
        """用于「未带 data_source_id 时自动建/复用 DataSource」（计划第 524–525 行）。"""
        return list(
            self.session.execute(
                self.scoped(project_id).where(DataSource.format == fmt).order_by(DataSource.id)
            ).scalars().all()
        )

    def find_one_by_format(self, project_id: int, fmt: str) -> DataSource | None:
        return self.session.execute(
            self.scoped(project_id)
            .where(DataSource.format == fmt)
            .order_by(DataSource.id)
            .limit(1)
        ).scalar_one_or_none()
