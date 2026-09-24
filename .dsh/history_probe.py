"""探针：历史相似事故**真的**被注入 Context 了吗。

阶段 09 验收里有一句「历史相似事故被注入 Context」。这一条此前完全不成立：
`PipelineInput.historical_incidents` 在 app/ 里没有任何赋值处（永远传空），
而 `incidents` 表也是空的 —— 既没有历史可注入，也没有地方去取。

本探针在**已有事故的项目**里再发起一次分析（同一份日志、同一个 project），
然后看两件事：
  ① Worker 日志里 `run_context_sources` 的 historical_incidents 计数（> 0 才算注入）；
  ② 这次的归并是否复用了历史事故（`incidents_reused` > 0，来自 `grouping_persisted`）。

用法：python .dsh/history_probe.py [project_id]
"""

from __future__ import annotations

import http.cookiejar
import json
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
EVIDENCE = ROOT / ".dsh" / "runtime_evidence.json"
PASSWORD = "Passw0rd!23"  # 与 real_path_evidence.py 注册时用的一致
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
        return exc.code, {"detail": exc.read()[:200].decode("utf-8", "replace")}


def worker_log_facts(run_id: int) -> dict:
    """从容器 Worker 日志里取这次 Run 的关键事实（经 docker compose logs）。

    为什么证据在日志里而不是库里：`historical_incidents` 与 `incidents_reused`
    是**注入与复用的计数**，它们只出现在 Worker 的结构化日志里。
    不解析它们，"历史事故有没有真的注入"就只能靠读代码猜。
    """
    import re
    import subprocess

    facts: dict = {}
    try:
        proc = subprocess.run(
            ["docker", "compose", "logs", "worker", "--tail", "400"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - 没有 docker 就退化为"只看接口"
        return {"log_unavailable": f"{type(exc).__name__}: {exc}"}

    for line in (proc.stdout or "").splitlines():
        if f'"run_id": {run_id}' not in line:
            continue
        for key in ("run_context_sources", "grouping_persisted"):
            if f'"event": "{key}"' in line:
                match = re.search(r"\{.*\}", line)
                if not match:
                    continue
                try:
                    facts[key] = json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue
    return facts


def main(argv: list[str]) -> int:
    import sqlalchemy as sa

    from app.config import get_settings

    eng = sa.create_engine(str(get_settings().database_url))
    with eng.connect() as conn:
        if argv:
            project_id = int(argv[0])
        else:
            project_id = int(
                conn.execute(
                    sa.text("select project_id from incidents order by id desc limit 1")
                ).scalar_one()
            )
        row = conn.execute(
            sa.text(
                "select p.id, p.user_id, u.email,"
                " (select id from data_sources ds where ds.project_id = p.id limit 1) as source_id"
                " from projects p join users u on u.id = p.user_id where p.id = :p"
            ),
            {"p": project_id},
        ).one()
        before = conn.execute(
            sa.text("select count(*) from incidents where project_id = :p"), {"p": project_id}
        ).scalar_one()

    project_id, _, email, source_id = row
    print(f"项目 {project_id}（owner {email}，数据源 {source_id}），已有事故 {before} 条")

    status, login = call("POST", "/api/login", {"email": email, "password": PASSWORD})
    token = login.get("access_token") or login.get("token")
    if not token:
        print(f"登录失败（{status}）：{login}")
        return 1

    status, created = call(
        "POST",
        f"/api/projects/{project_id}/analysis-runs",
        {"data_source_id": int(source_id), "start_tier": "L2"},
        token,
    )
    run_id = created.get("run_id")
    print(f"第二次分析 Run {run_id}（HTTP {status}）")
    terminal = {"completed", "partial_success", "failed", "timeout", "cancelled"}
    final = {}
    for _ in range(60):
        time.sleep(2)
        _, final = call("GET", f"/api/runs/{run_id}?project_id={project_id}", token=token)
        if final.get("status") in terminal:
            break

    with eng.connect() as conn:
        after = conn.execute(
            sa.text("select count(*) from incidents where project_id = :p"), {"p": project_id}
        ).scalar_one()
        reused_note = conn.execute(
            sa.text(
                "select count(*) from incidents where project_id = :p and source_run_id = :r"
            ),
            {"p": project_id, "r": run_id},
        ).scalar_one()

    facts = {
        "project_id": project_id,
        "second_run_id": run_id,
        "final_status": final.get("status"),
        "incidents_before": int(before),
        "incidents_after": int(after),
        "incidents_created_by_second_run": int(reused_note),
        "worker_log": worker_log_facts(int(run_id)),
        "note": "historical_incidents 的计数在 worker_log.run_context_sources；"
        "incidents_reused 在 worker_log.grouping_persisted",
    }
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    payload["history_probe"] = facts
    EVIDENCE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
