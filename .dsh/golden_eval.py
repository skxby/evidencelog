"""golden-set-run —— 跑 5 个场景并打印评测报告（离线、零成本：router=None）。

复用 tests/golden 的 runner，不另写一套判定 —— 另写一套测的就不是 CI 跑的东西。
用法：
    python .dsh/golden_eval.py            # 离线（零成本）
    RUN_GOLDEN_LIVE=1 python .dsh/golden_eval.py   # 真模型（付费，谨慎）
"""

from __future__ import annotations

import os
import pathlib
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Project, User  # noqa: E402
from app.repositories import ProjectRepository, UserRepository  # noqa: E402
from tests.golden.runner import SCENARIOS, run_all  # noqa: E402


def _live_router():
    """真模型 router：只有显式要求时才构造（会产生费用）。"""
    from app.config import get_settings
    from app.gateways.router import build_router

    return build_router(get_settings())


def main() -> int:
    live = os.environ.get("RUN_GOLDEN_LIVE") == "1"
    session = SessionLocal()
    try:
        user = UserRepository(session).add(
            User(
                email=f"gs-eval-{secrets.token_hex(4)}@example.com",
                password_hash="x",
            )
        )
        session.flush()
        projects = ProjectRepository(session)
        project_ids = {}
        for scenario in SCENARIOS:
            project = projects.add(
                Project(
                    user_id=user.id,
                    name=f"gs-{scenario}",
                    budget_total=10,
                    budget_used=0,
                )
            )
            session.flush()
            project_ids[scenario] = project.id
        session.commit()

        uploads_dir = pathlib.Path(".dsh/golden-uploads")
        report = run_all(
            session=session,
            project_ids=project_ids,
            uploads_dir=uploads_dir,
            router=_live_router() if live else None,
        )

        print(f"模式：{'真模型（付费）' if live else '离线（router=None，零成本）'}")
        print(report.render_table())
        print()
        print("逐条断言：")
        for check in report.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"  [{mark}] {check.scenario:<14} {check.assertion} — {check.detail}")

        print()
        print("逐场景明细：")
        for outcome in report.outcomes:
            print(
                f"  {outcome.scenario:<14} 解析 {outcome.parsed}/"
                f"{outcome.parsed + outcome.bad_lines}"
                f"  等级 {outcome.tier:<3} 结论 {outcome.insight_count}"
                f"  无证据 fact {outcome.facts_without_evidence}"
                f"  成本 ¥{outcome.cost:.6f}"
                f"  异常 {', '.join(outcome.anomaly_types) or '—'}"
                f"  {'纯规则' if outcome.rules_only else '含模型'}"
            )
        print()
        print(f"通过：{report.passed}；失败断言 {len(report.failures())} 条")
        print(f"合计成本 ¥{report.total_cost:.6f}")
        return 0 if report.passed else 1
    finally:
        session.rollback()
        session.close()
        with engine.begin() as conn:
            for table in ("evidences", "insights", "events", "agent_runs", "data_sources"):
                if table == "evidences":
                    conn.execute(
                        text(
                            "DELETE FROM evidences WHERE insight_id IN (SELECT id FROM insights "
                            "WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'gs-%'))"
                        )
                    )
                else:
                    conn.execute(
                        text(
                            f"DELETE FROM {table} WHERE project_id IN "
                            "(SELECT id FROM projects WHERE name LIKE 'gs-%')"
                        )
                    )
            conn.execute(text("DELETE FROM projects WHERE name LIKE 'gs-%'"))
            conn.execute(text("DELETE FROM users WHERE email LIKE 'gs-eval-%@example.com'"))


if __name__ == "__main__":
    raise SystemExit(main())
