"""补齐几条"待实测"的真机证据（每条都是**真实路径**，不是模拟）。

对应验收表里 ⚠️ 的三类场景：

  --migration   迁移可执行可回滚（在**临时库**上跑 upgrade → downgrade base → upgrade，
                不碰主库数据）
  --degrade     模型不可用时降级（把 Key 换成无效值，看 Run 是否如实收成
                partial_success + 纯规则报告，并检查错误分类）
  --partial-retry  极小单次上限触发 partial_success，再用「重新分析」建新 Run
                （需要把 DEFAULT_RUN_MAX_COST 调到"预检能过、实际会超"的区间）

用法（前两条零模型费用；第三条会产生一次真实调用）：
    python .dsh/edge_scenarios.py --migration
    EVIDENCE_BASE=http://127.0.0.1:8001 python .dsh/edge_scenarios.py --degrade
    EVIDENCE_BASE=http://127.0.0.1:8001 python .dsh/edge_scenarios.py --partial-retry
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("EVIDENCE_BASE", "http://127.0.0.1:8001")
EVIDENCE = ROOT / ".dsh" / "runtime_evidence.json"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(method: str, path: str, body=None, token=None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read()[:300].decode("utf-8", "replace")}


def merge(patch: dict) -> None:
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    payload.update(patch)
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    EVIDENCE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )


# ============================================================
# ① 迁移：临时库上 upgrade → downgrade base → upgrade
# ============================================================


def migration() -> int:
    import sqlalchemy as sa

    from app.config import get_settings

    settings = get_settings()
    url = str(settings.database_url)
    scratch = url.rsplit("/", 1)[0] + "/logagent_migtest"
    admin = sa.create_engine(url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("drop database if exists logagent_migtest"))
        conn.execute(sa.text("create database logagent_migtest"))

    def alembic(*args: str) -> tuple[int, str]:
        env = {**os.environ, "DATABASE_URL": scratch}
        proc = subprocess.run(
            [str(PY), "-m", "alembic", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def tables() -> int:
        engine = sa.create_engine(scratch)
        with engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text(
                        "select count(*) from information_schema.tables"
                        " where table_schema='public'"
                    )
                ).scalar_one()
            )

    up_code, up_log = alembic("upgrade", "head")
    after_up = tables()
    down_code, down_log = alembic("downgrade", "base")
    after_down = tables()
    re_code, re_log = alembic("upgrade", "head")
    after_re = tables()

    facts = {
        "scratch_database": "logagent_migtest",
        "upgrade_head": {"exit": up_code, "tables": after_up},
        "downgrade_base": {"exit": down_code, "tables": after_down},
        "upgrade_again": {"exit": re_code, "tables": after_re},
        # downgrade base 之后必然剩一张 `alembic_version`（版本表自己不清），
        # 所以判据是"业务表全没了"而不是"表数为 0"。
        "rollback_works": down_code == 0 and after_down <= 1 and after_re == after_up > 0,
        "tail": (down_log or up_log).strip().splitlines()[-3:],
    }
    merge({"migration_check": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0 if facts["rollback_works"] else 1


# ============================================================
# ② 模型不可用 → 降级（零费用：调用失败不计费）
# ============================================================


def _new_project_with_upload(token: str, name: str) -> tuple[int, int]:
    _, project = call("POST", "/api/projects", {"name": name, "budget_total": 10}, token)
    pid = project["id"]
    path = ROOT / "tests" / "datasets" / "crash" / "system.log"
    boundary = "----edge" + secrets.token_hex(8)
    body = bytearray()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    body += b"Content-Type: text/plain\r\n\r\n" + path.read_bytes() + b"\r\n"
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="fmt"\r\n\r\ntxt\r\n'.encode()
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/projects/{pid}/upload", data=bytes(body), method="POST"
    )
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with OPENER.open(req, timeout=180) as response:
        up = json.loads(response.read() or b"{}")
    return pid, int(up["data_source_id"])


def _poll(pid: int, run_id: int, token: str) -> dict:
    terminal = {"completed", "partial_success", "failed", "timeout", "cancelled"}
    final: dict = {}
    for _ in range(60):
        time.sleep(2)
        _, final = call("GET", f"/api/runs/{run_id}?project_id={pid}", token=token)
        if final.get("status") in terminal:
            break
    return final


def _register(prefix: str) -> str:
    _, reg = call(
        "POST",
        "/api/register",
        {"email": f"{prefix}-{secrets.token_hex(4)}@example.com", "password": "Passw0rd!23"},
    )
    return reg.get("access_token") or reg.get("token")


def degrade() -> int:
    """模型不可用：Run 必须**如实**收成 partial_success + 纯规则报告，并写明原因。"""
    token = _register("degrade")
    pid, sid = _new_project_with_upload(token, "edge-degrade")
    _, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": sid, "start_tier": "L2"},
        token,
    )
    run_id = created.get("run_id")
    final = _poll(pid, run_id, token)

    import sqlalchemy as sa

    from app.config import get_settings

    engine = sa.create_engine(str(get_settings().database_url))
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "select status, error, run_metadata, model_calls, cost_actual"
                " from agent_runs where id = :r"
            ),
            {"r": run_id},
        ).one()

    metadata = row[2] or {}
    # 错误分类留痕：每一次等级尝试里，重试历史都带 `error_kind`
    # （由 `classify_error` 判定）。它证明分类器在真实路径上跑过。
    kinds = sorted(
        {
            str(item.get("error_kind"))
            for attempt in (metadata.get("attempts") or [])
            for item in ((attempt.get("retry") or {}).get("history") or [])
            if item.get("error_kind")
        }
    )
    facts = {
        "run_id": run_id,
        "project_id": pid,
        "status": row[0],
        "stop_reason": metadata.get("stop_reason"),
        "note": str(metadata.get("note"))[:80],
        "used_rules_only": metadata.get("used_rules_only"),
        "error": str(row[1])[:160],
        "model_calls": len(row[3] or []),
        "cost": float(row[4] or 0),
        "error_kinds": kinds,
        "degraded_as_expected": row[0] == "partial_success"
        and metadata.get("stop_reason") == "model_unavailable",
    }
    merge({"degrade_check": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0 if facts["degraded_as_expected"] else 1


# ============================================================
# ③ 极小上限 → partial_success，再「重新分析」
# ============================================================


def partial_retry() -> int:
    token = _register("partial")
    pid, sid = _new_project_with_upload(token, "edge-partial")
    _, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": sid, "start_tier": "L2"},
        token,
    )
    run_id = created.get("run_id")
    final = _poll(pid, run_id, token)

    status, detail = call("GET", f"/api/runs/{run_id}/detail?project_id={pid}", token=token)
    narrative = (detail or {}).get("narrative", "") if isinstance(detail, dict) else ""

    retry_status, retried = call("POST", f"/api/runs/{run_id}/retry", token=token)
    new_run = retried.get("run_id") if isinstance(retried, dict) else None
    parent = None
    if new_run:
        import sqlalchemy as sa

        from app.config import get_settings

        engine = sa.create_engine(str(get_settings().database_url))
        with engine.connect() as conn:
            parent = conn.execute(
                sa.text("select parent_run_id from agent_runs where id = :r"), {"r": new_run}
            ).scalar_one()
        call("POST", f"/api/runs/{new_run}/cancel", token=token)

    facts = {
        "run_id": run_id,
        "project_id": pid,
        "final_status": final.get("status"),
        "run_metadata": {
            k: final.get(k) for k in ("status", "stop_reason", "cost_actual", "tokens_input")
        },
        "narrative_head": narrative[:180],
        "retry_http": retry_status,
        "retry_run_id": new_run,
        "retry_parent_run_id": parent,
        "partial_and_retry_ok": final.get("status") == "partial_success"
        and retry_status == 202
        and parent == run_id,
    }
    merge({"partial_retry_check": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1, default=str))
    return 0 if facts["partial_and_retry_ok"] else 1


def retry_existing(run_id: int) -> int:
    """对一条**可重试**的 Run 走「重新分析」：新 Run 必须指回原 Run（parent_run_id）。

    用哪条 Run 都能验：验收要的是"failed / partial_success / timeout 有重试入口"，
    所以拿一条真实产出的 partial_success（模型不可用那条）来跑最省。
    """
    import sqlalchemy as sa

    from app.config import get_settings

    engine = sa.create_engine(str(get_settings().database_url))
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("select project_id, status from agent_runs where id = :r"), {"r": run_id}
        ).one()
    project_id, status = int(row[0]), str(row[1])
    email = None
    with engine.connect() as conn:
        email = conn.execute(
            sa.text(
                "select u.email from projects p join users u on u.id = p.user_id where p.id = :p"
            ),
            {"p": project_id},
        ).scalar_one()

    _, login = call("POST", "/api/login", {"email": email, "password": "Passw0rd!23"})
    token = login.get("access_token") or login.get("token")
    http_status, retried = call("POST", f"/api/runs/{run_id}/retry", token=token)
    new_run = retried.get("run_id") if isinstance(retried, dict) else None
    parent = None
    if new_run:
        with engine.connect() as conn:
            parent = conn.execute(
                sa.text("select parent_run_id from agent_runs where id = :r"), {"r": new_run}
            ).scalar_one()
        # 立刻取消，别让它再花一次钱（能赶上就在模型调用前生效）
        call("POST", f"/api/runs/{new_run}/cancel", token=token)

    facts = {
        "source_run_id": run_id,
        "source_status": status,
        "retry_http": http_status,
        "retry_detail": str(retried.get("detail"))[:120] if isinstance(retried, dict) else "",
        "new_run_id": new_run,
        "new_run_parent": parent,
        "retry_entry_ok": http_status == 202 and parent == run_id,
    }
    merge({"retry_check": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0 if facts["retry_entry_ok"] else 1


def main(argv: list[str]) -> int:
    if "--migration" in argv:
        return migration()
    if "--degrade" in argv:
        return degrade()
    if "--partial-retry" in argv:
        return partial_retry()
    if "--retry-existing" in argv:
        index = argv.index("--retry-existing")
        return retry_existing(int(argv[index + 1]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
