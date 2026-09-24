"""真机接线图：从**真实入口点**出发做调用图可达性。

## 为什么需要它

阶段 07 的三道成本闸门、`runbook` 加载器、`parent_run_id` 都曾经满足同一个
条件：**代码在、单测全绿、`grep` 也能搜到**，但真实路径上一次都没被调用过。
`dead_wiring_audit.py` 抓得住"全 app 只有定义那一行"的形态，抓不住
**"被另一段同样没人调用的代码引用"** —— 死链。本工具补的就是这一层。

## 判据

先从真实入口点做种子，再顺着调用边向下走，能走到 = 已接线：

  入口点 = ① 被 `@router.get/post/...`、`@celery_app.task`、`@<signal>.connect`
              装饰的函数（HTTP 路由与 Worker 任务就是真机的两个入口）；
           ② `app.main` / `app.celery_app` 的**模块级语句**（进程启动时执行的那段：
              `configure_logging(...)`、`app.include_router(...)`、`mount_static(app)`）。

## 局限（必须知道，否则会把结论读过头）

调用目标靠**名字**解析，不做类型推断：

  - `router.generate(...)` 这类在参数上调方法的写法，只能按方法名匹配。
    因此解析结果分两档，报告里如实区分：
      `精确`  —— 唯一候选（含 `from X import y` 直接指到定义）；
      `同名 N` —— 同名候选有 N 个，其中至少一个可达（**比精确弱**）。
  - `getattr(obj, name)()`、字典派发等动态调用无法解析，会被计入 `动态未解析`。
    这类调用存在时，"未接入"的结论要人工复核一次。

一句话：本工具能**证明接上了**（有链可查），"没接上"的结论在有动态调用时
需要人工确认。宁可标弱，也不给一个看起来精确的假结论。
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections import deque
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"

#: 入口模块：它们的模块级语句在进程启动时执行。
ENTRY_MODULES = ("app.main", "app.celery_app")

#: 装饰器属性名 → 视为真实入口（HTTP 路由 / Celery 任务 / 信号处理）。
ENTRY_DECORATOR_ATTRS = {
    "get", "post", "put", "patch", "delete", "head", "options",  # FastAPI
    "task", "connect", "on_after_configure", "on_after_finalize",  # Celery
}

#: 框架按**名字引用**注册、由框架在运行时回调的东西：
#: `Depends(get_db)`、`add_middleware(TraceIdMiddleware)`、`errorhandler(AuthError)`。
#: 它们不是"被调用"，但确实是真实路径的一部分，漏掉就会报一堆假警。
FRAMEWORK_REGISTRARS = {
    "Depends", "add_middleware", "exception_handler", "errorhandler",
    "add_exception_handler", "include_router", "mount", "middleware",
}

#: `getattr(self, f"model_{tier}_price...")` 这种动态属性访问解析不出来。
#: 只有经过 `settings_usage_audit.py` 独立证明确实被读到的字段才允许登记在这里 ——
#: 否则这个名单就成了"把假警藏起来"的地方。
#: 注册式装配里的回调参数名：`make_spec(func=...)`、`register(handler=...)`。
#: 被注册的函数不是"被调用"，但从此成为可达能力 —— 不认它，
#: **按名调用**（`registry.call("stats_calculator")`）的符号会被误报成未接线。
CALLBACK_KEYWORD_NAMES = {"func", "handler", "hook", "factory", "callable", "fn"}

DYNAMIC_ACCESSOR_ALLOWLIST: dict[str, str] = {
    "app.config.Settings.tier_model": "经 settings_usage_audit 确认由 f-string getattr 读取",
    "app.config.Settings.tier_price": "经 settings_usage_audit 确认由 f-string getattr 读取",
    "app.config.Settings.tier_reasoning": "经 settings_usage_audit 确认由 f-string getattr 读取",
}


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass
class Hit:
    """一个符号的可达性结论。"""

    symbol: str
    reachable: bool
    quality: str  # 精确 / 同名 N / 未接入 / 不存在
    chain: list[str]

    def render(self) -> str:
        return f"{self.symbol} → {'接入' if self.reachable else '未接入'}（{self.quality}）"


class WiringGraph:
    def __init__(self) -> None:
        self.modules: dict[str, ast.Module] = {}
        self.defs: dict[str, ast.AST] = {}
        self.module_defs: dict[str, dict[str, str]] = {}
        self.methods_by_name: dict[str, set[str]] = {}
        self.imports: dict[str, dict[str, str]] = {}
        self.module_of: dict[str, str] = {}
        self.class_of: dict[str, str] = {}
        self.entries: list[str] = []
        self.startup: list[tuple[str, ast.AST]] = []
        self.properties: set[str] = set()
        self.parent: dict[str, str] = {}
        self.quality: dict[str, str] = {}
        self.reachable: set[str] = set()
        self.dynamic_unresolved: list[tuple[str, int]] = []

    # ---------- 解析 ----------

    def load(self) -> None:
        for path in sorted(APP.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            mod = _module_name(path)
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            self.modules[mod] = tree
            self.module_defs[mod] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qn = f"{mod}.{node.name}"
                    self.defs[qn] = node
                    self.module_defs[mod][node.name] = qn
                    self.module_of[qn] = mod
                elif isinstance(node, ast.ClassDef):
                    qn = f"{mod}.{node.name}"
                    self.defs[qn] = node
                    self.module_defs[mod][node.name] = qn
                    self.module_of[qn] = mod
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            mqn = f"{qn}.{sub.name}"
                            self.defs[mqn] = sub
                            self.module_of[mqn] = mod
                            self.class_of[mqn] = qn
                            self.methods_by_name.setdefault(sub.name, set()).add(mqn)
                            if any(
                                (d.id if isinstance(d, ast.Name) else getattr(d, "attr", ""))
                                == "property"
                                for d in sub.decorator_list
                            ):
                                self.properties.add(mqn)
            self.imports[mod] = self._imports_of(tree)

    @staticmethod
    def _imports_of(tree: ast.Module) -> dict[str, str]:
        """alias → 限定名。函数体内的 import 也算（它们是真实执行到的）。"""
        out: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    out[name] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                base = node.module or ""
                for alias in node.names:
                    target = f"{base}.{alias.name}" if base else alias.name
                    out[alias.asname or alias.name] = target
        return out

    def _dotted(self, node: ast.AST) -> str | None:
        """把 `a.b.c` 这样的属性链还原成点号字符串（不解析导入）。"""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
        return None

    def _as_module(self, dotted: str, module: str) -> str | None:
        """把点号字符串解析成一个**模块名**（含 `import app.tools.data_ops` 形态）。"""
        if dotted in self.modules:
            return dotted
        head, _, rest = dotted.partition(".")
        target = self.imports.get(module, {}).get(head)
        if target is None:
            return None
        if not rest:
            return target if target in self.modules else None
        candidate = f"{target}.{rest}"
        return candidate if candidate in self.modules else None

    def _methods_of_class(self, class_qn: str, attr: str) -> list[str]:
        out = [f"{class_qn}.{attr}"] if f"{class_qn}.{attr}" in self.defs else []
        return out

    def resolve(self, node: ast.Call, *, module: str, cls: str | None) -> tuple[list[str], str]:
        """解析一次调用 → (候选限定名, 质量)。质量 ∈ {精确, 同名 N, 未解析}。"""
        func = node.func

        if isinstance(func, ast.Name):
            name = func.id
            local = self.module_defs.get(module, {}).get(name)
            if local:
                return [local], "精确"
            imported = self.imports.get(module, {}).get(name)
            if imported and imported in self.defs:
                return [imported], "精确"
            if imported and imported in self.modules:
                return [], "未解析"  # 调用的是模块本身，不是函数
            same = [m for m, names in self.module_defs.items() if name in names]
            cands = [f"{m}.{name}" for m in sorted(same)]
            return self._quality(cands, name, kind="函数")
        if isinstance(func, ast.Attribute):
            attr = func.attr
            base = func.value

            # `self.method(...)`
            if isinstance(base, ast.Name) and base.id == "self" and cls:
                own = self._methods_of_class(cls, attr)
                if own:
                    return own, "精确"
                return self._by_method_name(cls, attr)

            # `module.func(...)` —— 含 `a.b.c.func(...)` 形态
            dotted = self._dotted(base)
            if dotted is not None:
                as_mod = self._as_module(dotted, module)
                if as_mod:
                    qn = f"{as_mod}.{attr}"
                    if qn in self.defs:
                        return [qn], "精确"
                imported = self.imports.get(module, {}).get(dotted)
                if imported:
                    # 导入的是类/函数：`Foo.bar` 是类方法或静态方法
                    qn = f"{imported}.{attr}"
                    if qn in self.defs:
                        return [qn], "精确"
                    if imported in self.defs:
                        return self._by_method_name(imported, attr)
                head = dotted.split(".")[0]
                if head in self.imports.get(module, {}):
                    resolved = self.imports[module][head]
                    qn = f"{resolved}.{attr}"
                    if qn in self.defs:
                        return [qn], "精确"

            # `Foo().bar(...)` / 工厂函数返回值上调用
            if isinstance(base, ast.Call):
                inner, _ = self.resolve(base, module=module, cls=cls)
                for cand in inner:
                    own = self._methods_of_class(cand, attr)
                    if own:
                        return own, "精确"

            return self._by_method_name(None, attr)

        return [], "未解析"

    def _by_method_name(self, cls: str | None, attr: str) -> tuple[list[str], str]:
        cands = set(self.methods_by_name.get(attr, set()))
        if cls:
            # 同名方法里若包含本类的方法，优先本类（继承链最常见的情形）
            own = {c for c in cands if c.startswith(cls + ".")}
            if own:
                return sorted(own), "精确"
        return self._quality(sorted(cands), attr, kind="方法")

    @staticmethod
    def _quality(cands: list[str], name: str, *, kind: str) -> tuple[list[str], str]:
        if not cands:
            return [], "未解析"
        if len(cands) == 1:
            return cands, "精确"
        return cands, f"同名 {len(cands)} 个 {kind}"

    # ---------- 遍历 ----------

    def _reachable_modules(self) -> set[str]:
        """从入口模块沿 import 边走到的模块 —— 它们的**模块级语句**都会执行。

        漏掉这一条的后果实测过：`logger = get_logger(__name__)`、
        ORM 模型类体里的 `mapped_column(...)`、`templates = get_templates()`
        全被报成"未接线"，几十行假警把真警淹了。
        """
        seen: set[str] = set()
        queue: deque[str] = deque(m for m in ENTRY_MODULES if m in self.modules)
        while queue:
            mod = queue.popleft()
            if mod in seen:
                continue
            seen.add(mod)
            for target in self.imports.get(mod, {}).values():
                if target in self.modules:
                    queue.append(target)
                    continue
                parent = target.rsplit(".", 1)[0]
                if parent in self.modules:
                    queue.append(parent)
        return seen

    def _collect_entries(self) -> None:
        self.startup: list[tuple[str, ast.AST]] = []
        for mod, tree in self.modules.items():
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if self._is_entry_decorated(node):
                        self.entries.append(f"{mod}.{node.name}")
        for mod in sorted(self._reachable_modules()):
            for node in self.modules[mod].body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                self.startup.append((mod, node))

    @staticmethod
    def _is_entry_decorated(node: ast.AST) -> bool:
        for dec in getattr(node, "decorator_list", []):
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr in ENTRY_DECORATOR_ATTRS:
                return True
        return False

    def build(self) -> None:
        self._collect_entries()
        queue: deque[str] = deque()

        def push(dst: str, parent: str, quality: str) -> None:
            if dst in self.parent:
                return
            self.parent[dst] = parent
            self.quality[dst] = quality
            queue.append(dst)

        for qn in self.entries:
            push(qn, "<入口>", "精确")
        for mod, node in self.startup:
            for dst, quality in self._edges_in(node, module=mod, cls=None):
                push(dst, f"{mod}:<模块级>", quality)

        while queue:
            qn = queue.popleft()
            self.reachable.add(qn)
            node = self.defs.get(qn)
            if node is None:
                continue
            for dst, quality in self._edges_in(
                node, module=self.module_of[qn], cls=self.class_of.get(qn)
            ):
                push(dst, qn, quality)

        for qn, why in DYNAMIC_ACCESSOR_ALLOWLIST.items():
            if qn in self.defs:
                push(qn, f"<动态访问白名单：{why}>", "白名单")

    def _edges_in(
        self, node: ast.AST, *, module: str, cls: str | None
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if isinstance(node, ast.ClassDef):
            # 类被"用到"（实例化或被注解引用）时，真正执行的是类体语句与 __init__；
            # 其余方法要等**被调用**才算接线 —— 否则一个类被 import 一次，
            # 它所有方法里的调用都会跟着变"可达"，缺口就被盖住了。
            parts = [
                n
                for n in node.body
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            init = next(
                (
                    n
                    for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "__init__"
                ),
                None,
            )
            if init is not None:
                parts.append(init)
            for part in parts:
                out.extend(self._edges_in(part, module=module, cls=cls))
            return out
        for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
            cands, quality = self.resolve(call, module=module, cls=cls)
            if quality == "未解析":
                self.dynamic_unresolved.append((module, getattr(call, "lineno", 0)))
                if self._is_framework_registrar(call):
                    for name in self._arg_names(call):
                        out.extend(
                            (c, q) for c, q in self._resolve_name(name, module)
                        )
            out.extend((c, quality) for c in cands)
            # 注册式装配：`make_spec(func=stats_calculator)`、`register(handler=...)`。
            # 被注册的函数**不是被调用**，但从这一刻起它就是可达能力的一部分 ——
            # 不认这一条，工具会因为"经注册表按名调用"而被误报成未接线
            # （按名调用本身解析不出来，只能靠注册点这一条线索）。
            for keyword in call.keywords:
                if keyword.arg in CALLBACK_KEYWORD_NAMES:
                    for name in [
                        n.id for n in ast.walk(keyword.value) if isinstance(n, ast.Name)
                    ]:
                        out.extend(self._resolve_name(name, module))
        # 签名注解 / 默认值 / 基类里的名字引用：Pydantic 模型、异常类、
        # Protocol 基类都是"被引用即接线"，不是被调用。
        for name in self._name_refs(node):
            out.extend(self._resolve_name(name, module))
        # 局部/类级变量注解：`rows: list[EventGroup] = ...`
        for ann in [n for n in ast.walk(node) if isinstance(n, ast.AnnAssign)]:
            if ann.annotation is None:
                continue
            for name in [n.id for n in ast.walk(ann.annotation) if isinstance(n, ast.Name)]:
                out.extend(self._resolve_name(name, module))
        # `@property` 是**访问**不是调用：`self.tokens` / `ctx.budget` 每读一次
        # 就在跑一次函数体。不认这一条，属性会整批被报成"未接线"。
        if self.properties:
            for attr in [n for n in ast.walk(node) if isinstance(n, ast.Attribute)]:
                for qn in sorted(self.properties):
                    if qn.rsplit(".", 1)[-1] == attr.attr:
                        out.append((qn, "属性访问"))
        return out

    @staticmethod
    def _is_framework_registrar(call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in FRAMEWORK_REGISTRARS
        if isinstance(func, ast.Attribute):
            return func.attr in FRAMEWORK_REGISTRARS
        return False

    @staticmethod
    def _arg_names(call: ast.Call) -> list[str]:
        nodes: list[ast.AST] = [*call.args]
        nodes.extend(kw.value for kw in call.keywords if kw.value is not None)
        names: list[str] = []
        for arg in nodes:
            if isinstance(arg, ast.Name):
                names.append(arg.id)
            elif isinstance(arg, ast.Call):
                names.extend(
                    n.id for n in [*arg.args] if isinstance(n, ast.Name)
                )
        return names

    @staticmethod
    def _name_refs(node: ast.AST) -> list[str]:
        found: list[str] = []
        targets: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            targets.extend([node.args, node.returns])
            targets.extend(node.decorator_list)
            targets.extend(d for d in node.args.defaults if d is not None)
            targets.extend(d for d in node.args.kw_defaults if d is not None)
        elif isinstance(node, ast.ClassDef):
            targets.extend(node.bases)
            targets.extend(node.decorator_list)
            targets.extend(kw.value for kw in node.keywords if kw.value is not None)
        for target in targets:
            if target is None:
                continue
            found.extend(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        return sorted(set(found))

    def _resolve_name(self, name: str, module: str) -> list[tuple[str, str]]:
        local = self.module_defs.get(module, {}).get(name)
        if local:
            return [(local, "精确")]
        imported = self.imports.get(module, {}).get(name)
        if imported and imported in self.defs:
            return [(imported, "精确")]
        same = [
            f"{m}.{name}" for m in sorted(self.module_defs) if name in self.module_defs[m]
        ]
        cands, quality = self._quality(same, name, kind="函数")
        return [(c, quality) for c in cands]

    # ---------- 查询 ----------

    def chain_of(self, symbol: str) -> list[str]:
        chain: list[str] = []
        cur: str | None = symbol
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self.parent.get(cur)
        return list(reversed(chain))

    def hit(self, symbol: str) -> Hit:
        if symbol not in self.defs:
            return Hit(symbol, False, "不存在（名字对不上）", [])
        if symbol in self.reachable:
            return Hit(symbol, True, self.quality.get(symbol, "精确"), self.chain_of(symbol))
        return Hit(symbol, False, "未接入（无人从入口点调到）", [])

    def summary(self) -> dict[str, int]:
        return {
            "入口点": len(self.entries),
            "可达符号": len(self.reachable),
            "已定义符号": len(self.defs),
            "动态未解析调用": len(self.dynamic_unresolved),
        }


def plain(symbol: str) -> str:
    """`app.policy.wiring.build_cost_controller` → `build_cost_controller`。"""
    return symbol.rsplit(".", 1)[-1]


def _corpus(directory: pathlib.Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(directory.rglob("*.py"))
        if "__pycache__" not in p.parts
    )


def gaps(graph: WiringGraph) -> dict[str, list[str]]:
    """**穷举**"定义了但入口点走不到"的符号，按危险程度分三桶。

    这是本工具真正的用法：不是拿几个可疑符号去查，而是每次全量扫一遍，
    免得又漏掉一个阶段 07。分桶的意义在于处理顺序 ——
    "被测试覆盖但没接线"最危险：它意味着**测试正在替一个真机上不存在的行为背书**。
    """
    import re

    tests_corpus = _corpus(ROOT / "tests")
    app_corpus = _corpus(APP)

    covered_by_tests: list[str] = []
    referenced_in_app: list[str] = []
    nobody: list[str] = []

    for qn in sorted(graph.defs):
        if qn in graph.reachable:
            continue
        name = plain(qn)
        if name.startswith("_") or name in {"__init__", "__post_init__"}:
            continue
        if name in {"model_config", "__repr__", "__eq__", "__hash__"}:
            continue
        hits = len(re.findall(r"\b" + re.escape(name) + r"\b", app_corpus))
        in_tests = bool(re.search(r"\b" + re.escape(name) + r"\b", tests_corpus))
        if hits <= 1 and in_tests:
            covered_by_tests.append(qn)
        elif hits > 1:
            referenced_in_app.append(qn)
        elif in_tests:
            covered_by_tests.append(qn)
        else:
            nobody.append(qn)
    return {
        "被测试覆盖但未接线": covered_by_tests,
        "被 app 内引用但那条链自己不可达": referenced_in_app,
        "没人引用": nobody,
    }


def main(argv: list[str]) -> int:
    graph = WiringGraph()
    graph.load()
    graph.build()

    print("## 真机接线图")
    print()
    for key, value in graph.summary().items():
        print(f"- {key}：{value}")
    print()

    if "--gaps" in argv:
        buckets = gaps(graph)
        for title, rows in buckets.items():
            print(f"### {title}（{len(rows)}）")
            print()
            if not rows:
                print("无")
                print()
                continue
            print("| 符号 | 定义处 | 现在谁在调 |")
            print("|---|---|---|")
            for qn in rows:
                chain = " → ".join(plain(x) for x in graph.chain_of(qn)) or "—"
                where = qn.rsplit(".", 1)[0]
                print(f"| `{plain(qn)}` | `{where}` | {chain} |")
            print()
        argv = [a for a in argv if a != "--gaps"]
        if not argv:
            return 0

    print("| 符号 | 结论 | 质量 | 从入口点起的调用链 |")
    print("|---|---|---|---|")
    for raw in argv:
        symbol = raw if raw in graph.defs else _guess(graph, raw)
        hit = graph.hit(symbol)
        chain = " → ".join(plain(x) for x in hit.chain) if hit.chain else "—"
        verdict = "接入" if hit.reachable else "未接入"
        print(f"| `{plain(symbol)}` | {verdict} | {hit.quality} | {chain} |")
    return 0


def _guess(graph: WiringGraph, name: str) -> str:
    """允许用短名查询：唯一匹配才认，否则原样返回（结论会是"不存在"）。"""
    matches = [qn for qn in graph.defs if plain(qn) == name]
    return matches[0] if len(matches) == 1 else name


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
