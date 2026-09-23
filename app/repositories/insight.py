"""Insight 仓库。"""

from __future__ import annotations

from app.models.insight import Insight
from app.repositories.base import ProjectScopedRepository


class InsightRepository(ProjectScopedRepository[Insight]):
    model = Insight

    def list_for_run(self, project_id: int, run_id: int) -> list[Insight]:
        return list(
            self.session.execute(
                self.scoped(project_id).where(Insight.run_id == run_id).order_by(Insight.id)
            ).scalars().all()
        )

    def list_for_incident(self, project_id: int, incident_id: int) -> list[Insight]:
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Insight.incident_id == incident_id)
                .order_by(Insight.id)
            ).scalars().all()
        )
