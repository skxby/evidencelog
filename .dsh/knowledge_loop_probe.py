"""探针：知识闭环是否真的闭上了（候选 → 人工确认 → 下次分析命中）。

计划 8.1 / 第 22 节第 13 条要的是：新异常沉淀为候选 → 人工确认 → **下次能命中**，
误报能被标记并抑制。这条链此前断在两处：
  - 候选从来没写进 staging（`write_candidates` 没有生产调用方）；
  - confirmed 知识从来没被加载进分析（`confirmed_knowledge` 在 app/ 内无赋值处）。
两处都修完之后，本探针走**真实 HTTP + 真实 Worker**跑一遍完整闭环：
  ① 读 staging 候选（知识审核接口）
  ② 人工确认其中一条（确认接口）
  ③ 再发起一次分析 → 看 Worker 日志里 `run_context_sources.confirmed_knowledge`

用法：python .dsh/knowledge_loop_probe.py [project_id]
日志解析需要 docker compose 可用（容器栈）；不可用时会退化为"只看接口结果"。
"""

from __future__ import annotations

import http.cookiejar
import json
import pathlib
import re
import subprocess
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
        return exc.code, {"detail": exc.read()[:300].decode("utf-8", "replace")}


def owner_of(project_id: int) -> tuple[str, int]:
    import sqlalchemy as sa

    from app.config import get_settings

    eng = sa.create_engine(str(get_settings().database_url))
    with eng.connect() as conn:
        row = conn.execute(
            sa.text(
                "select u.email,"
                " (select id from data_sources ds where ds.project_id = p.id limit 1)"
                " from projects p join users u on u.id = p.user_id where p.id = :p"
            ),
            {"p": project_id},
        ).one()
    return str(row[0]), int(row[1])


def worker_log_facts(run_id: int) -> dict:
    """从容器 Worker 日志里取这次 Run 的两条关键事实（经 docker compose logs）。"""
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
        if f'"run_id": {run_id}' not in line and f"'run_id': {run_id}" not in line:
            continue
        for key in ("run_context_sources", "grouping_persisted"):
            if f'"event": "{key}"' in line:
                match = re.search(r"\{.*\}", line)
                if not match:
                    continue
                try:
                    payload = json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue
                facts[key] = payload
    return facts


def main(argv: list[str]) -> int:
    project_id = int(argv[0]) if argv else 7873
    email, source_id = owner_of(project_id)
    print(f"项目 {project_id}（owner {email}，数据源 {source_id}）")

    status, login = call("POST", "/api/login", {"email": email, "password": PASSWORD})
    token = login.get("access_token") or login.get("token")
    if not token:
        print(f"登录失败（{status}）：{login}")
        return 1

    status, candidates = call(
        "GET", f"/api/projects/{project_id}/knowledge/candidates?project_id={project_id}",
        token=token,
    )
    if not isinstance(candidates, list) or not candidates:
        print(f"staging 里没有候选（{status}）：{candidates}")
        return 1
    target = candidates[0]
    print(f"staging 候选 {len(candidates)} 条，确认第一条：{target.get('id')} / {target.get('title')}")

    status, confirm = call(
        "POST",
        f"/api/knowledge/candidates/{target['id']}/confirm?project_id={project_id}",
        token=token,
    )
    print(f"确认接口 → {status} {confirm}")

    status, created = call(
        "POST",
        f"/api/projects/{project_id}/analysis-runs",
        {"data_source_id": source_id, "start_tier": "L2"},
        token,
    )
    run_id = created.get("run_id")
    terminal = {"completed", "partial_success", "failed", "timeout", "cancelled"}
    final = {}
    for _ in range(60):
        time.sleep(2)
        _, final = call("GET", f"/api/runs/{run_id}?project_id={project_id}", token=token)
        if final.get("status") in terminal:
            break

    status, after = call(
        "GET", f"/api/projects/{project_id}/knowledge/candidates?project_id={project_id}",
        token=token,
    )
    logs = worker_log_facts(int(run_id))

    facts = {
        "project_id": project_id,
        "confirmed_candidate": target.get("id"),
        "confirm_http": status,
        "confirm_response": confirm,
        "candidates_before": len(candidates),
        "candidates_after": len(after) if isinstance(after, list) else after,
        "third_run_id": run_id,
        "third_run_status": final.get("status"),
        "worker_log": logs,
        "confirmed_knowledge_loaded": (logs.get("run_context_sources") or {}).get(
            "confirmed_knowledge"
        ),
    }
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    payload["knowledge_loop_probe"] = facts
    EVIDENCE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
