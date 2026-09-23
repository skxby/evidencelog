"""AgentRun 仓库。

验收要点（计划第 365 行）：`model_calls` 可追加记录。

`model_calls` 是 JSON 数组，追加 = 读-改-写。并发下这一步需要行级锁，
故 `append_model_call` 用 `with_for_update()` 取行——避免两次调用互相覆盖
（静默丢数据比报错更危险，红线 4）。
"""

from __future__ import annotations

from typing import Any

from app.models import enums
from app.models.agent_run import AgentRun
from app.repositories.base import ProjectScopedRepository


class AgentRunRepository(ProjectScopedRepository[AgentRun]):
    model = AgentRun

    def find_by_idempotency_key(
        self, project_id: int, idempotency_key: str
    ) -> AgentRun | None:
        """幂等复用：相同键且已有成功 Run → 直接复用结果（计划第 699–703 行）。"""
        return self.session.execute(
            self.scoped(project_id).where(
                AgentRun.idempotency_key == idempotency_key,
                AgentRun.status == enums.AGENT_RUN_COMPLETED,
            )
        ).scalar_one_or_none()

    def append_model_call(self, project_id: int, run_id: int, call: dict[str, Any]) -> bool:
        """向 `model_calls` 追加一条记录。

        用行级锁串行化，防止并发追加时后写覆盖先写。
        """
        run = self.session.execute(
            self.scoped(project_id).where(AgentRun.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run is None:
            return False
        # JSONB 列的可变对象需要重新赋值才会被标记为脏
        calls = list(run.model_calls or [])
        calls.append(call)
        run.model_calls = calls
        return True

    def model_calls_of(self, project_id: int, run_id: int) -> list[dict[str, Any]]:
        run = self.get(project_id, run_id)
        return list(run.model_calls or []) if run is not None else []
