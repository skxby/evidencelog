"""真机核验成本闸门（零成本：被拒的路径不花钱，放行的那次跑 L0 也不花钱）。

跑在真实 HTTP 上，覆盖三项：
  ① 预算 < 一次 L3 估计 → 创建被拒 402
  ② 预算 0.00001（小于金额精度）→ 422，而不是被四舍五入成 0（=不设上限）
  ③ 不填预算（0）→ 视为未设上限，正常放行
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=90) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    email = f"eval-budget-{secrets.token_hex(4)}@example.com"
    _, reg = call("POST", "/api/register", {"email": email, "password": "Passw0rd!23"})
    token = reg.get("access_token") or reg.get("token")
    window = {
        "start": "2026-09-23T12:00:00+00:00",
        "end": "2026-09-23T13:00:00+00:00",
    }

    # ① 预算太小 → 拒绝创建
    _, project = call("POST", "/api/projects", {"name": "budget-tiny", "budget_total": 0.001}, token)
    pid = project["id"]
    _, source = call("POST", f"/api/projects/{pid}/data-sources", {"format": "txt"}, token)
    status, body = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": source["id"], "start_tier": "L3", "time_range": window},
        token,
    )
    print(f"① 预算 0.001 创建 Run → {status} | {str(body.get('detail'))[:110]}")

    # ② 低于金额精度 → 422
    status, body = call(
        "POST", "/api/projects", {"name": "budget-too-small", "budget_total": 0.00001}, token
    )
    detail = str(body.get("detail", ""))
    print(f"② 预算 0.00001 → {status} | {detail[:90]}")

    # ③ 不填预算 → 放行
    _, project2 = call("POST", "/api/projects", {"name": "budget-unset"}, token)
    _, source2 = call("POST", f"/api/projects/{project2['id']}/data-sources", {"format": "txt"}, token)
    status, body = call(
        "POST",
        f"/api/projects/{project2['id']}/analysis-runs",
        {"data_source_id": source2["id"], "time_range": window},
        token,
    )
    print(f"③ 不填预算创建 Run → {status} | {json.dumps(body, ensure_ascii=False)[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
