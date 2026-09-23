"""FastAPI 入口。

阶段 10 起：业务端点走 `app/api/` 下的路由模块；API 只做验证、鉴权、
创建任务、返回状态，**不承担分析逻辑**（计划第 856 行）。
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.api.routes_auth_projects import router as auth_projects_router
from app.api.routes_knowledge import router as knowledge_router
from app.api.routes_runs import router as runs_router
from app.config import get_settings
from app.db import engine

settings = get_settings()

app = FastAPI(
    title="Log Intelligence Agent",
    version="0.1.0",
    description="个人全栈、单领域、只读的日志分析 Agent Runtime（V1）",
)

app.include_router(auth_projects_router)
app.include_router(runs_router)
app.include_router(knowledge_router)


@app.get("/healthz")
def healthz() -> dict:
    """存活检查：进程在不在。故意不碰外部依赖，避免依赖抖动引起误判重启。"""
    return {"status": "ok", "environment": settings.environment}


@app.get("/healthz/deps")
def healthz_deps() -> JSONResponse:
    """就绪检查：PostgreSQL 与 Redis 是否真的可用。"""
    checks: dict[str, dict] = {}

    try:
        with engine.connect() as conn:
            checks["postgres"] = {
                "ok": True,
                "server_version": conn.execute(text("SHOW server_version")).scalar_one(),
            }
    except Exception as exc:  # noqa: BLE001 — 就绪探针必须捕获一切并如实上报
        checks["postgres"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    client = None
    try:
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        client.ping()
        checks["redis"] = {"ok": True, "version": client.info().get("redis_version")}
    except Exception as exc:  # noqa: BLE001 — 就绪探针必须捕获一切并如实上报
        checks["redis"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            client.close()

    healthy = all(item["ok"] for item in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
