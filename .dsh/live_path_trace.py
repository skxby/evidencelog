"""真机路径追踪：记录一次真实分析**实际执行了 app/ 里哪些函数**。

## 为什么不能用静态可达性代替

`wiring_graph.py` 回答的是"这个符号从入口点**能不能**被调到"（静态推断，
靠名字解析）。本次排查要回答的是另一个问题：**这一次真机运行里，它到底跑了没有**。
两者的差距正是阶段 07 的教训 —— `CostController` 静态上"写全了"，运行时一次没进。

## 怎么用

```bash
# 终端 A：带追踪跑 Web（端口另开一个，避免与容器里的 8000 冲突）
python .dsh/live_path_trace.py web 8001
# 终端 B：带追踪跑 Worker（Broker 换到 Redis 的另一个 db，
#         免得任务被容器里的 worker 抢走）
$env:REDIS_URL="redis://127.0.0.1:6379/1"; python .dsh/live_path_trace.py worker
```

跑完后得到 `.dsh/trace_web.json` / `.dsh/trace_worker.json`：
`{"<相对路径>::<函数名>": 调用次数}`。计数为 0（不在表里）就是**这次真机没跑到**。

## 局限

- 只记**函数调用**，不记行覆盖；装饰器包装、C 扩展内部不可见。
- `sys.settrace` 会让执行变慢（本项目链路短，实测可接受）。
- 追踪到的是"这次场景"执行到的函数。没执行 ≠ 永远不会执行 ——
  例如异常分支、取消路径要专门造场景才会进。报告里按
  "本次实测执行 / 本次未触发" 两档如实区分，不写成"不存在"。
"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import signal
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
APP_PREFIX = str(APP).lower()

_calls: dict[str, int] = {}
_lock = threading.Lock()
_role = "unknown"
_started = time.time()


def _tracer(frame, event, arg):  # noqa: ANN001 - settrace 协议固定
    if event == "call":
        code = frame.f_code
        filename = code.co_filename
        if filename.lower().startswith(APP_PREFIX):
            try:
                rel = pathlib.Path(filename).resolve().relative_to(ROOT).as_posix()
            except ValueError:
                rel = filename
            key = f"{rel}::{code.co_name}"
            with _lock:
                _calls[key] = _calls.get(key, 0) + 1
    return _tracer


def install() -> None:
    threading.settrace(_tracer)
    sys.settrace(_tracer)
    atexit.register(dump)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass
    threading.Thread(target=_periodic_dump, daemon=True).start()


def _on_signal(signum, frame):  # noqa: ANN001
    dump()
    raise SystemExit(0)


def _periodic_dump() -> None:
    """周期性落盘：被强杀（kill -9 / 容器 stop 超时）时也留得下数据。"""
    while True:
        time.sleep(15)
        dump()


def dump() -> None:
    with _lock:
        payload = {
            "role": _role,
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_started)),
            "dumped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "call_count": sum(_calls.values()),
            "calls": dict(sorted(_calls.items())),
        }
    path = ROOT / ".dsh" / f"trace_{_role}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str]) -> int:
    global _role
    if not argv or argv[0] not in {"web", "worker"}:
        print(__doc__)
        return 2
    _role = argv[0]
    if _role == "web":
        port = int(argv[1]) if len(argv) > 1 else 8001
        import uvicorn

        install()
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
        return 0

    install()
    from celery.__main__ import main as celery_main

    sys.argv = [
        "celery", "-A", "app.celery_app", "worker", "-P", "solo", "-l", "info",
        "-Q", "celery",
    ]
    celery_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
