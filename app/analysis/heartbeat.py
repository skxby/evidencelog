"""心跳、僵尸回收与取消检查（计划第 705–710 行）。

```text
心跳与僵尸回收：Worker 在各阶段及模型调用前后更新 last_heartbeat；
Celery Beat 每周期检查，心跳丢失超过阈值（如 5 分钟，阈值要大于单次最慢调用）
的 running Run 置为 timeout。

取消：置 cancel_requested 标记，Worker 在阶段边界与每次调用前检查并抛出取消。
诚实说明粒度：若恰好进入一次长调用，最坏要等该调用返回，不承诺「秒停」。
```

**阈值必须大于单次最慢调用**（计划第 706 行），否则正在正常跑长调用的 Run
会被误判成僵尸并杀掉——那比不回收更糟。故默认阈值与网关超时联动校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: 心跳丢失阈值（秒）。计划第 706 行举例 5 分钟。
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 300

#: 网关单次调用超时（与 app.gateways.deepseek.DEFAULT_TIMEOUT_SECONDS 同口径）。
#: 心跳阈值必须显著大于它。
GATEWAY_TIMEOUT_SECONDS = 120


class CancelledError(Exception):
    """Run 被取消。Worker 应在阶段边界捕获它并置 cancelled。

    刻意不是 `NonRetryableError` 的子类：取消不是"错误"，不该被重试逻辑
    当成失败来重试（计划第 716 行的不可重试清单里也没有它）。
    """

    def __init__(self, message: str = "Run 已被取消") -> None:
        super().__init__(message)


def validate_heartbeat_timeout(
    timeout_seconds: int, *, gateway_timeout_seconds: float = GATEWAY_TIMEOUT_SECONDS
) -> None:
    """阈值必须大于单次最慢调用，否则会误杀正在正常工作的 Run。

    计划第 706 行括号里那句「阈值要大于单次最慢调用」不是建议，是硬约束：
    小于它就会出现「模型还在正常返回，Run 已被判为僵尸」。
    """
    if timeout_seconds <= 0:
        raise ValueError(f"心跳阈值必须为正，收到 {timeout_seconds}")
    if timeout_seconds <= gateway_timeout_seconds:
        raise ValueError(
            f"心跳阈值 {timeout_seconds}s 不大于单次最慢调用 {gateway_timeout_seconds}s；"
            "这会把正在正常跑长调用的 Run 误判为僵尸"
        )


@dataclass(frozen=True)
class HeartbeatRecord:
    """一个 Run 的心跳快照（从库里读出来的最小字段）。"""

    run_id: int
    project_id: int
    status: str
    last_heartbeat: datetime | None
    started_at: datetime | None
    cancel_requested: bool = False


def is_zombie(
    record: HeartbeatRecord,
    *,
    now: datetime,
    timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
) -> bool:
    """判断一个 running 的 Run 是否已成僵尸。

    只看 `running`：`queued` 的 Run 还没有 Worker 接手，它没有心跳是正常的，
    不该按心跳判定（那种情况由「排队超时」处理，语义不同）。
    """
    from app.models.enums import AGENT_RUN_RUNNING

    if record.status != AGENT_RUN_RUNNING:
        return False

    reference = record.last_heartbeat or record.started_at
    if reference is None:
        # running 却既无心跳也无开始时间 —— 这是数据异常，按僵尸处理并让它
        # 变成 timeout，比无限挂着好（挂着的 Run 会永远占着"运行中"）。
        return True

    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return (now - reference) > timedelta(seconds=timeout_seconds)


def heartbeat_deadline(
    *, started_at: datetime, timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
) -> datetime:
    """该 Run 的心跳截止时刻，便于页面显示"还有多久被回收"。"""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at + timedelta(seconds=timeout_seconds)


def should_abort(*, cancel_requested: bool) -> bool:
    """Worker 在阶段边界与每次调用前调它；返回 True 就抛 `CancelledError`。"""
    return bool(cancel_requested)


def check_cancelled(*, cancel_requested: bool) -> None:
    """检查点：需要中止就抛 `CancelledError`。"""
    if should_abort(cancel_requested=cancel_requested):
        raise CancelledError()
