"""探针：runbook 在真机上到底挂没挂上、没挂上的原因是什么。

现象（阶段 03/11 验收）：真机跑完的结论里 `run_metadata` 全是 null，
一条 runbook 都没出现。

本探针取**真机那次 Run 已经落库的事件**（不是合成数据），用与生产同一条函数链
（`run_detection` → `attach_runbooks`）重放一遍，把每条候选异常的
「命中词 → 生成的 message → runbook 查询结果」打出来。

用法：python .dsh/runbook_probe.py [project_id]
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.analysis.pipeline import attach_runbooks, run_detection  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.domains.wiring import build_default_registry  # noqa: E402
from app.repositories.event import EventRepository  # noqa: E402


def main(argv: list[str]) -> int:
    project_id = int(argv[0]) if argv else 6944  # 崩溃样本那次真机 Run 的项目
    domain = build_default_registry().load("computer_monitoring")
    session = SessionLocal()
    events = EventRepository(session).pipeline_events(project_id)
    session.close()
    print(f"项目 {project_id}：从库里取回真机事件 {len(events)} 条")

    anomalies, _ = run_detection(events, analyzers=domain.analyzers())
    print(f"确定性检测产出候选异常 {len(anomalies)} 条")
    attached = attach_runbooks(anomalies, domain=domain)
    print(f"attach_runbooks 挂上 {attached} 条")
    print()

    for anomaly in anomalies[:5]:
        message = str(anomaly.get("message", ""))
        book = domain.find_runbook_for(
            str(anomaly.get("type", "")), message, anomaly.get("metric_name")
        )
        print(f"· 类型={anomaly.get('type')} 命中词={(anomaly.get('detail') or {}).get('pattern')!r}")
        print(f"  生成 message={message[:90]!r}")
        print(f"  find_runbook_for → {getattr(book, 'id', None)}")
    if not anomalies:
        print("（本次无候选异常）")

    all_books = {b.id: b.applies for b in domain.runbooks()}
    print()
    print("已加载 runbook 及其 applies：")
    for rid, applies in sorted(all_books.items()):
        print(f"  {rid}: {applies}")

    # 并入真机证据，供 .dsh/stage_acceptance.py 直接引用
    import json

    evidence = ROOT / ".dsh" / "runtime_evidence.json"
    payload = json.loads(evidence.read_text(encoding="utf-8")) if evidence.exists() else {}
    sample = anomalies[0] if anomalies else {}
    payload["runbook_probe"] = {
        "project_id": project_id,
        "events": len(events),
        "anomalies": len(anomalies),
        "attached": attached,
        "sample": {
            "type": sample.get("type"),
            "pattern": (sample.get("detail") or {}).get("pattern"),
            "message": str(sample.get("message", ""))[:160],
            "runbook": None,
        },
        "runbooks": all_books,
    }
    evidence.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n已并入 {evidence.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
