"""结构化日志与 trace_id（计划第 925–938 行）。

```text
trace_id（一次请求 / 一次 Run 一个，贯穿日志与任务）
```

**为什么 trace_id 必须走 contextvars 而不是参数传递**：日志调用散布在
路由、Worker、网关、流水线各处，靠参数一路透传会让每层函数签名都被污染，
而且漏传一处就静默断链。contextvars 让"当前在哪个 Run 上"成为一个隐式但
可靠的作用域，进程内任何地方取到的都是同一个值。

structlog 输出 JSON（计划第 937 行），便于 grep 与后续接集中式平台。
OpenTelemetry 属 V1 之后（计划第 938 行），这里只做到"可追踪"。
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar, Token
from typing import Any, Self

import structlog

#: 当前 trace_id。默认 None 表示"不在某个 Run 的上下文里"。
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
#: 当前 run_id，便于日志直接带上（计划第 929 行要求记录 run_id）
_run_id_var: ContextVar[int | None] = ContextVar("run_id", default=None)
#: 当前 project_id
_project_id_var: ContextVar[int | None] = ContextVar("project_id", default=None)

TRACE_ID_HEADER = "X-Trace-Id"


def new_trace_id() -> str:
    """生成一个 trace_id。

    用 uuid4 的十六进制而不是自增/时间戳：多个进程各自产生 id 时不会撞车，
    也不会泄露"这是第几次请求"这类信息。
    """
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def current_run_id() -> int | None:
    return _run_id_var.get()


def current_project_id() -> int | None:
    return _project_id_var.get()


class bind_run_context:
    """把 trace_id / run_id / project_id 绑定到当前上下文（可作上下文管理器）。

    退出时**恢复原值**而不是清空：Worker 里可能嵌套调用，恢复比清空正确。
    """

    def __init__(
        self,
        *,
        trace_id: str | None = None,
        run_id: int | None = None,
        project_id: int | None = None,
    ) -> None:
        self.trace_id = trace_id or new_trace_id()
        self.run_id = run_id
        self.project_id = project_id
        self._tokens: list[tuple[ContextVar[Any], Token[Any]]] = []

    def __enter__(self) -> Self:
        self._tokens.append((_trace_id_var, _trace_id_var.set(self.trace_id)))
        if self.run_id is not None:
            self._tokens.append((_run_id_var, _run_id_var.set(self.run_id)))
        if self.project_id is not None:
            self._tokens.append((_project_id_var, _project_id_var.set(self.project_id)))
        return self

    def __exit__(self, *exc_info: object) -> None:
        for var, token in reversed(self._tokens):
            var.reset(token)
        self._tokens.clear()


def _add_context(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """给每条日志补上上下文里的 trace_id / run_id / project_id。"""
    trace_id = current_trace_id()
    if trace_id is not None:
        event_dict.setdefault("trace_id", trace_id)
    run_id = current_run_id()
    if run_id is not None:
        event_dict.setdefault("run_id", run_id)
    project_id = current_project_id()
    if project_id is not None:
        event_dict.setdefault("project_id", project_id)
    return event_dict


_configured = False


def configure_logging(*, level: str = "INFO", force: bool = False) -> None:
    """配置 structlog：JSON 输出 + trace_id 注入。

    幂等：重复调用不会叠加 handler（否则每条日志会被打印多次，
    那种"日志重复"很容易被误判成 bug 反复触发）。
    """
    global _configured
    if _configured and not force:
        return

    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # JSON 输出（计划第 937 行）：可 grep、可接集中式平台
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """取结构化 logger。首次调用会自动配置。"""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


def log_event(event: str, **fields: Any) -> None:
    """记一条结构化事件。找不到 logger 时不应让业务失败。"""
    get_logger().info(event, **fields)
