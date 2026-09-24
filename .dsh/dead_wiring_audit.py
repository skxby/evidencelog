"""dead-wiring-audit —— 找出"写好了但没接上"的符号。

本轮反复咬人的就是这一类：`runbook` 加载器、`CostController`、
`parent_run_id`、`RunOutcome.insight_count`、`DEFAULT_RUN_MAX_COST`、
`MODEL_CACHE_HIT_INPUT_PRICE_PER_1M` —— 单测全绿、代码全在，
**真实路径上一次都没被调用过**。逐个碰运气太慢，这里一次扫完。

判据（**宁可少报也不能乱报** —— 一个乱报警的工具比没有更糟：
本工具第一版判据太粗，把 FastAPI 的路由处理函数、同模块内的私有辅助
全报成了死代码，74 条里绝大多数是假的）：

  一个顶层函数/类被判"没人用"，必须同时满足：
    ① 在整个 `app/` 语料里**只出现它自己的定义那一行**（含本模块内部）；
    ② `tests/` 里也没有引用；
    ③ 没有装饰器 —— 路由处理函数是被 `@router.get(...)` 注册的，
       名字不会被别处引用，那不是接线缺口。

  另外单独列一类："app 内无人引用、但 tests 在用"——
  这正是"控制器级测试"的形态（功能对、就是没接上），
  `CostController` 当初就是这样躲过所有测试的。

用法：python .dsh/dead_wiring_audit.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
TESTS = ROOT / "tests"
SKIP_PARTS = {"__pycache__"}

IGNORE_NAMES = {
    "__init__", "__post_init__", "__repr__", "__eq__", "__hash__", "model_config",
}


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py") if not (SKIP_PARTS & set(p.parts)))


def _top_level(tree: ast.Module) -> list[tuple[str, int, bool]]:
    """(名字, 行号, 是否有装饰器)。"""
    out: list[tuple[str, int, bool]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in IGNORE_NAMES or node.name.startswith("_"):
                continue
            out.append((node.name, node.lineno, bool(node.decorator_list)))
    return out


def main() -> int:
    modules = _modules()
    app_text = {p: p.read_text(encoding="utf-8", errors="replace") for p in modules}
    app_corpus = "\n".join(app_text.values())
    tests_corpus = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in TESTS.rglob("*.py")
        if not (SKIP_PARTS & set(p.parts))
    )

    dead: list[tuple[str, str, int]] = []
    test_only: list[tuple[str, str, int]] = []

    for path in modules:
        try:
            tree = ast.parse(app_text[path])
        except SyntaxError:
            continue
        rel = str(path.relative_to(ROOT))
        for name, lineno, decorated in _top_level(tree):
            pattern = re.compile(r"\b" + re.escape(name) + r"\b")
            hits = len(pattern.findall(app_corpus))
            # 定义那一行本身算一次；>1 说明别处（含本模块内部）用过
            if hits > 1 or decorated:
                continue
            (test_only if pattern.search(tests_corpus) else dead).append(
                (rel, name, lineno)
            )

    def dump(title: str, rows: list[tuple[str, str, int]], note: str) -> None:
        print(f"## {title}（{len(rows)}）")
        print()
        if not rows:
            print("无")
            print()
            return
        print("| 文件 | 符号 | 行 |")
        print("|---|---|---|")
        for rel, name, lineno in rows:
            print(f"| {rel} | `{name}` | {lineno} |")
        print()
        print(note)
        print()

    dump(
        "app 与 tests 都没人用（多半是死代码）",
        dead,
        "这些可以直接删；留着会让读代码的人以为有地方在用。",
    )
    dump(
        "只有 tests 在用（典型形态：控制器级测试，功能对但没接上）",
        test_only,
        "逐个确认：要么接进真实路径，要么承认它只是测试辅助并注明。",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
