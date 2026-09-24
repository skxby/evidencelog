"""查看某个 Run 的当前状态（真机复验用的小工具）。

用法：python .dsh/run_status.py [run_id ...]
不带参数时看最近 5 条 Run。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402

SQL = """
select id,
       status,
       started_at,
       last_heartbeat,
       round(extract(epoch from (now() - coalesce(last_heartbeat, started_at)))) as hb_age_s,
       cost_actual,
       run_metadata->>'stop_reason' as stop_reason,
       left(coalesce(error, ''), 60) as error
from agent_runs
{where}
order by id
"""


def main(argv: list[str]) -> int:
    eng = create_engine(str(get_settings().database_url))
    if argv:
        where = "where id in (" + ",".join(str(int(a)) for a in argv) + ")"
    else:
        where = "where id in (select id from agent_runs order by id desc limit 5)"
    with eng.connect() as conn:
        print("now =", conn.execute(text("select now()")).scalar_one())
        print(
            f"{'id':>6} {'status':<16} {'started_at':<26} {'hb_age_s':>9} "
            f"{'cost':>9}  stop_reason / error"
        )
        for row in conn.execute(text(SQL.format(where=where))):
            print(
                f"{row[0]:>6} {row[1]:<16} {str(row[2]):<26} {str(row[4]):>9} "
                f"{float(row[5] or 0):>9.6f}  {row[6] or ''} {row[7] or ''}"
            )
            print(f"        last_heartbeat={row[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
