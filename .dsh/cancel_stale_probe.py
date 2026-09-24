"""验证「取消为什么在真机上不生效」的机制：**Worker 的长事务把取消写入挡在门外**。

现象（`.dsh/real_path_evidence.py --cancel-test`）：分析进行到一半时调取消，
API 返回 200、`cancel_requested=true` 也确实出现在库里，但那次 Run 已经跑完了
（跑了多久，取消就等了多久）—— 取消永远慢一步。

机制（本脚本用真实数据库复现，不调模型、不改业务代码）：

  `app/tasks/analysis.py` 的 Worker **整个任务只用一个 Session**，
  从第一次 `apply_status(running)` / `heartbeat()` 起就在 `agent_runs` 那一行上
  持有写锁，直到任务结束才 `session.commit()`（第 296 行）。
  于是 API 进程的 `UPDATE agent_runs SET cancel_requested=true`（`request_cancel`）
  只能在 Worker 提交之后才拿到锁 —— 而那时分析早已结束。

复现步骤：
  ① 会话 A 写一次心跳但不提交（模拟 Worker 的长事务，行锁在手）；
  ② 另一个连接带 `lock_timeout` 去改 `cancel_requested` → 会抛锁等待超时；
  ③ 会话 A 提交后再试同一句 → 立刻成功。

用法：python .dsh/cancel_stale_probe.py [run_id]
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.repositories.agent_run import AgentRunRepository  # noqa: E402


def latest_run_id() -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("select max(id) from agent_runs")).scalar_one())


def main(argv: list[str]) -> int:
    run_id = int(argv[0]) if argv else latest_run_id()
    with engine.connect() as conn:
        project_id, original = conn.execute(
            text("select project_id, cancel_requested from agent_runs where id = :r"),
            {"r": run_id},
        ).one()
    print(f"取用 Run {run_id}（project {project_id}，当前 cancel_requested={original}）")

    session_a = SessionLocal()
    repo_a = AgentRunRepository(session_a)
    repo_a.touch_heartbeat(project_id, run_id)
    session_a.flush()
    print("① 会话 A 写心跳并 flush（未提交）—— 这就是 Worker 整个任务期间的常态")

    blocked = False
    try:
        with engine.begin() as conn:
            conn.execute(text("set local lock_timeout = '3s'"))
            conn.execute(
                text("update agent_runs set cancel_requested = :v where id = :r"),
                {"v": not original, "r": run_id},
            )
    except OperationalError as exc:
        blocked = True
        print(f"② 另一个连接的取消写入被挡住：{type(exc).__name__}: {str(exc).splitlines()[0]}")
    if not blocked:
        print("② 取消写入**没有**被挡住 —— 机制不成立，需要另找原因")

    session_a.commit()
    session_a.close()
    with engine.begin() as conn:
        conn.execute(
            text("update agent_runs set cancel_requested = :v where id = :r"),
            {"v": original, "r": run_id},
        )
    print("③ 会话 A 提交后再写入 → 成功（取消这时才生效，而 Run 早已跑完）")
    print()
    print(
        "结论："
        + (
            "机制成立 —— 取消的 UPDATE 被 Worker 未提交的长事务阻塞，"
            "只能在 Worker 提交后落地。"
            if blocked
            else "机制不成立 —— 需要在别处找原因。"
        )
    )
    return 0 if blocked else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
