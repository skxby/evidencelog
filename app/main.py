"""FastAPI 入口。

阶段 01 只做两件事：证明服务能起来、证明它真的连得上 PostgreSQL 与 Redis。
分析逻辑一行都没有，那是阶段 09 的事。
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

settings = get_settings()

app = FastAPI(title="Log Intelligence Agent", version="0.1.0")


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
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    client = None
    try:
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        client.ping()
        checks["redis"] = {"ok": True, "version": client.info().get("redis_version")}
    except Exception as exc:
        checks["redis"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            client.close()

    healthy = all(item["ok"] for item in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
