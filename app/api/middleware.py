"""trace_id 中间件（计划第 928 行：一次请求一个 trace_id，贯穿日志与任务）。

行为：
- 请求带 `X-Trace-Id` 就用它（便于从网关/前端串联），否则新生成一个；
- 把 trace_id 绑进上下文，使本次请求内所有结构化日志自动带上；
- **响应里回传 `X-Trace-Id`**，这样用户遇到问题时能把 id 报给你去 grep 日志。

刻意不把 trace_id 写进数据库：`AgentRun` 的 trace_id 由创建 Run 的那次请求
决定并随 Run 一起持久化（见 routes_runs），中间件只负责"当前作用域"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.utils.observability import TRACE_ID_HEADER, bind_run_context, new_trace_id


class TraceIdMiddleware(BaseHTTPMiddleware):
    """给每个请求绑定 trace_id，并在响应头回传。"""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(TRACE_ID_HEADER)
        trace_id = incoming.strip() if incoming and incoming.strip() else new_trace_id()

        with bind_run_context(trace_id=trace_id):
            response = await call_next(request)
            response.headers[TRACE_ID_HEADER] = trace_id
            return response
