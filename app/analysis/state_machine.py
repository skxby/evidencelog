"""AgentRun 7 态状态机（计划第 679–693 行）。

```text
queued    → running
running   → completed | partial_success | failed | timeout | cancelled
queued    → cancelled

拦截非法转移：
completed / partial_success / failed / timeout / cancelled → 不可再改
failed 不可原地复活，只能新建 Run（parent_run_id 关联）
```

**本模块是状态转移的唯一真源。** 任何地方想改 `AgentRun.status` 都应经
`StateMachine.transition()`，否则「不可再改」这条就没有机制保障。
"""

from __future__ import annotations

from app.models.enums import (
    AGENT_RUN_CANCELLED,
    AGENT_RUN_COMPLETED,
    AGENT_RUN_FAILED,
    AGENT_RUN_PARTIAL_SUCCESS,
    AGENT_RUN_QUEUED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_STATES,
    AGENT_RUN_TERMINAL_STATES,
    AGENT_RUN_TIMEOUT,
)


class IllegalTransitionError(ValueError):
    """非法的状态转移。"""


#: 合法转移表。**只列计划明写的转移**，外加一处显式标注的补充（见下方说明）。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    AGENT_RUN_QUEUED: frozenset(
        {
            AGENT_RUN_RUNNING,
            AGENT_RUN_CANCELLED,
            # 计划未明写：Worker 始终没来取（Celery 积压 / 队列丢失）时，
            # 这个 Run 会永远停在 queued，僵尸回收也就无从触发。这里补一条
            # queued → timeout，使"排到超时"能被如实表达。属**对计划空白的补充**，
            # 不是对既有转移的修改；阶段 08 报告中已标注。
            AGENT_RUN_TIMEOUT,
        }
    ),
    AGENT_RUN_RUNNING: frozenset(
        {
            AGENT_RUN_COMPLETED,
            AGENT_RUN_PARTIAL_SUCCESS,
            AGENT_RUN_FAILED,
            AGENT_RUN_TIMEOUT,
            AGENT_RUN_CANCELLED,
        }
    ),
    # 终态：不可再改
    AGENT_RUN_COMPLETED: frozenset(),
    AGENT_RUN_PARTIAL_SUCCESS: frozenset(),
    AGENT_RUN_FAILED: frozenset(),
    AGENT_RUN_TIMEOUT: frozenset(),
    AGENT_RUN_CANCELLED: frozenset(),
}


def assert_known_status(status: str) -> None:
    if status not in AGENT_RUN_STATES:
        raise IllegalTransitionError(
            f"未知状态 {status!r}，只接受 {AGENT_RUN_STATES}"
        )


def is_terminal(status: str) -> bool:
    return status in AGENT_RUN_TERMINAL_STATES


def can_transition(current: str, target: str) -> bool:
    assert_known_status(current)
    assert_known_status(target)
    return target in ALLOWED_TRANSITIONS[current]


def allowed_targets(current: str) -> frozenset[str]:
    assert_known_status(current)
    return ALLOWED_TRANSITIONS[current]


class StateMachine:
    """管理一个 Run 的状态与阶段进度。

    刻意**不改数据库**：本类只管内存中的状态判定，落库由调用方经 repository
    完成（保持 Project 边界与事务控制在一个地方）。
    """

    def __init__(
        self,
        *,
        status: str = AGENT_RUN_QUEUED,
        current_phase: str | None = None,
        phase_history: list[dict] | None = None,
    ) -> None:
        assert_known_status(status)
        self._status = status
        self.current_phase = current_phase
        self.phase_history: list[dict] = list(phase_history or [])

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._status)

    def transition(self, target: str, *, reason: str | None = None, at: str | None = None) -> str:
        """执行一次转移，返回新状态。非法转移抛 `IllegalTransitionError`。"""
        if not can_transition(self._status, target):
            if self.is_terminal:
                raise IllegalTransitionError(
                    f"{self._status} 是终态，不可再改为 {target}"
                    + ("（failed 不可原地复活，只能新建 Run）" if self._status == AGENT_RUN_FAILED else "")
                )
            raise IllegalTransitionError(
                f"不允许从 {self._status} 转移到 {target}；"
                f"允许的目标：{sorted(allowed_targets(self._status))}"
            )

        previous, self._status = self._status, target
        self._append_phase(
            phase=self.current_phase or target,
            status=target,
            extra={"from": previous, **({"reason": reason} if reason else {})},
            at=at,
        )
        return self._status

    def start(self, *, phase: str | None = None, at: str | None = None) -> str:
        """queued → running。"""
        if phase is not None:
            self.current_phase = phase
        return self.transition(AGENT_RUN_RUNNING, at=at)

    def enter_phase(self, phase: str, *, at: str | None = None) -> None:
        """推进阶段进度。

        `current_phase` 与 `status` **正交**（计划第 354 行）：前者管"跑到哪一步"，
        后者管生命周期。故推进阶段不触发状态转移。
        """
        if self.is_terminal:
            raise IllegalTransitionError(
                f"Run 已处于终态 {self._status}，不应再推进阶段 {phase!r}"
            )
        self.current_phase = phase
        self._append_phase(phase=phase, status="done", at=at)

    def complete_phase(self, phase: str, *, at: str | None = None) -> None:
        self._append_phase(phase=phase, status="done", at=at)

    def skip_phase(self, phase: str, *, reason: str = "") -> None:
        self._append_phase(phase=phase, status="skipped", extra={"reason": reason} if reason else {})

    def completed_phases(self) -> list[str]:
        return [e["phase"] for e in self.phase_history if e.get("status") == "done"]

    def skipped_phases(self) -> list[str]:
        return [e["phase"] for e in self.phase_history if e.get("status") == "skipped"]

    def _append_phase(
        self,
        *,
        phase: str,
        status: str,
        extra: dict | None = None,
        at: str | None = None,
    ) -> None:
        from datetime import datetime, timezone

        entry: dict = {
            "phase": phase,
            "status": status,
            "at": at or datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            entry.update(extra)
        self.phase_history.append(entry)
