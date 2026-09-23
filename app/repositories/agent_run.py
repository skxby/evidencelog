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

    def append_tool_usage(self, project_id: int, run_id: int, usage: dict[str, Any]) -> bool:
        """把一次工具执行记录追加到 `tool_usage`（阶段 05 验收：执行结果可记录到 Run）。

        `tool_usage` 是 JSONB，故写入前先确认可序列化：工具输出里可能混入
        `datetime` 之类的对象，直接塞进去会在 flush 时抛 TypeError
        （阶段 04 已经在这上面栽过一次）。不可序列化时**明确报错**，
        而不是让脏数据悄悄进库。
        """
        import json

        try:
            json.dumps(usage)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"tool_usage 含有不可 JSON 序列化的内容：{type(exc).__name__}: {exc}"
            ) from exc

        run = self.session.execute(
            self.scoped(project_id).where(AgentRun.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run is None:
            return False

        existing = run.tool_usage or {}
        # 约定结构：{"<tool_name>": [<每次执行的记录>, ...]}
        tool_name = str(usage.get("tool", "unknown"))
        history = list(existing.get(tool_name, []))
        history.append(usage)
        run.tool_usage = {**existing, tool_name: history}
        return True

    def tool_usage_of(self, project_id: int, run_id: int) -> dict[str, Any]:
        run = self.get(project_id, run_id)
        return dict(run.tool_usage or {}) if run is not None else {}
