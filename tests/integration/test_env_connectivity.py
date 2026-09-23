"""环境连通性验收（阶段 01 验收第 1 条）。

需要先 `docker compose up -d`。用 -m "not integration" 可以跳过。
"""

import pytest
import redis as redis_lib
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

pytestmark = pytest.mark.integration


def test_postgres_version_meets_plan_requirement():
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar_one()
    major = int(str(version).split(".")[0])
    assert major >= 15, f"计划要求 PostgreSQL 15+，实际 {version}"


def test_postgres_jsonb_roundtrip():
    """Event.payload 依赖 JSONB，这里直接验一遍读写语义。"""
    payload = '{"metric_name": "cpu_used", "value": 92.4, "unit": "%"}'
    with engine.connect() as conn:
        conn.execute(text("CREATE TEMP TABLE probe_event (id int primary key, payload jsonb)"))
        conn.execute(
            text("INSERT INTO probe_event (id, payload) VALUES (1, CAST(:p AS jsonb))"),
            {"p": payload},
        )
        name, value = conn.execute(
            text("SELECT payload->>'metric_name', (payload->>'value')::float FROM probe_event")
        ).one()
        conn.rollback()

    assert name == "cpu_used"
    assert value == pytest.approx(92.4)


def test_postgres_timezone_is_utc():
    """容器 TZ 必须是 UTC，否则时间戳归一化的前提就不成立。"""
    with engine.connect() as conn:
        tz = conn.execute(text("SHOW timezone")).scalar_one()
    assert tz in {"UTC", "Etc/UTC"}, f"容器时区应为 UTC，实际 {tz}"


def test_redis_version_meets_plan_requirement():
    settings = get_settings()
    client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
    try:
        assert client.ping() is True
        version = client.info()["redis_version"]
    finally:
        client.close()

    assert int(str(version).split(".")[0]) >= 7, f"计划要求 Redis 7+，实际 {version}"


def test_redis_supports_celery_roundtrip():
    """Celery 的 broker/backend 都是 Redis，验一下基本键值往返。"""
    settings = get_settings()
    client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
    try:
        client.set("logagent:probe", "ok", ex=10)
        assert client.get("logagent:probe") == b"ok"
        client.delete("logagent:probe")
    finally:
        client.close()
