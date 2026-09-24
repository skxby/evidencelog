r"""阶段 11 验收的**契约**测试：前端真的能打到后端。

验收原文是「从上传到看报告**全程不碰命令行**」。此前的验证只到
"登录页返回 200" —— 那只证明页面能渲染，**完全没有证明页面上的按钮能打到后端**。
路径写错一个字母（`/api/analysis-runs` 写成 `/api/analyse-runs`）时：
页面照常渲染、所有测试照常通过、按钮点下去只弹一句报错 ——
正是本项目反复出现的那一类"代码都在、就是不生效"。

这里把「JS 调用的每个 path」与「OpenAPI 里真实存在的 route」逐条对齐。
两个坑都是实测踩出来的，写在代码里免得后人重踩：

1. **不能遍历 `app.routes`**：这个 FastAPI 版本把 `include_router` 的结果包成
   `_IncludedRouter`，`path`/`methods` 全是 None —— 遍历只能拿到 6 条，
   于是把 17 处正常调用全判成错误（本工具第一版就是这么"报警"的）。
   OpenAPI schema 是扁平的，且本来就是对外契约，拿它当基准。
2. **路径可能是拼接出来的**：`knowledge.html` 里先 `const base = \`.../${id}\``
   再 `\`${base}/confirm${q}\``。只认 `API.xxx('字面量')` 会漏掉这类调用，
   反过来把"其实有界面入口"的接口误报成"前端从未调用"。
   故先把 `const NAME = \`模板\`` 收集起来做一层替换，再匹配。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.main import app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "app" / "web"

#: 只在 JS 里出现的动态变量（如查询串），替换后即可当路径比对
_IGNORED_VARS = {"q", "qs", "query"}

_CONST_TEMPLATE_RE = re.compile(r"const\s+([A-Za-z_$][\w$]*)\s*=\s*`([^`]*)`")
_CALL_RE = re.compile(r"API\.(get|post|patch|upload)\s*\(")
_INDIRECT_RE = re.compile(r"\b(?:call|act|post|send)\s*\(\s*`([^`]*)`")


def _routes() -> list[tuple[str, str]]:
    """(方法, 路径) 列表，取自 OpenAPI（见模块说明第 1 条）。"""
    out: list[tuple[str, str]] = []
    for path, operations in app.openapi().get("paths", {}).items():
        for method in operations:
            if method.upper() not in {"HEAD", "OPTIONS", "PARAMETERS"}:
                out.append((method.upper(), path))
    return sorted(out)


def _read_args(text: str, start: int) -> str:
    """从 `(` 之后读到配对的 `)`，允许多行。"""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start:i]


def _first_template(body: str) -> str | None:
    match = re.search(r"`([^`]*)`|'([^']*)'|\"([^\"]*)\"", body)
    if not match:
        return None
    return next(g for g in match.groups() if g is not None)


def _resolve(raw: str, consts: dict[str, str]) -> str:
    """把 `${base}` 这类拼接还原成完整路径模板。"""
    text = raw
    for _ in range(5):  # 允许嵌套替换，但别无限循环
        before = text
        for name, value in consts.items():
            text = text.replace("${" + name + "}", value)
        if text == before:
            break
    return text


def _ui_calls() -> list[tuple[str, str, str, int]]:
    """(文件, 方法, 原始路径, 行号)。"""
    calls: list[tuple[str, str, str, int]] = []
    targets = sorted(WEB_DIR.glob("templates/*.html")) + sorted(WEB_DIR.glob("static/*.js"))
    for path in targets:
        text = path.read_text(encoding="utf-8")
        consts = {name: value for name, value in _CONST_TEMPLATE_RE.findall(text)}

        for match in _CALL_RE.finditer(text):
            method = match.group(1).upper()
            method = "POST" if method == "UPLOAD" else method
            raw = _first_template(_read_args(text, match.end()))
            if raw is None:
                continue
            calls.append(
                (path.name, method, _resolve(raw, consts),
                 text[: match.start()].count("\n") + 1)
            )

        # 间接调用：`call(`${base}/confirm${q}`, ...)` —— 路径是字符串实参，
        # 不经过 API.xxx()，只认上面的正则会把它们全漏掉（见模块说明第 2 条）
        for match in _INDIRECT_RE.finditer(text):
            calls.append(
                (path.name, "POST", _resolve(match.group(1), consts),
                 text[: match.start()].count("\n") + 1)
            )
    return calls


def _segments(path: str) -> list[str]:
    return [s for s in path.split("?")[0].strip("/").split("/") if s]


def _matches(call_path: str, route_path: str) -> bool:
    """两侧逐段比较；`${...}` 与 `{param}` 都算任意一段。"""
    left, right = _segments(call_path), _segments(route_path)
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a.startswith("${") or (b.startswith("{") and b.endswith("}")):
            continue
        if a != b:
            return False
    return True


@pytest.fixture(scope="module")
def routes() -> list[tuple[str, str]]:
    return _routes()


def test_route_table_is_not_silently_empty(routes):
    """基准表为空的话，下面的断言会"全部通过"——先把这条钉住。"""
    assert len(routes) >= 20, (
        f"只取到 {len(routes)} 条路由，说明取路由的方式又坏了"
        "（别退回去遍历 app.routes，见模块说明）"
    )


def test_every_ui_call_hits_a_real_route(routes):
    """前端每一处调用都必须对得上真实路由。"""
    unresolved: list[str] = []
    checked = 0
    for source, method, raw, line in _ui_calls():
        # 纯变量拼接（拿不到字面量）的不在这里判，由下一条测试覆盖
        if not raw.startswith("/"):
            unresolved.append(f"{source}:{line} {method} {raw}")
            continue
        checked += 1
        assert any(
            m == method and _matches(raw, p) for m, p in routes
        ), f"{source}:{line} 调用了后端不存在的路由：{method} {raw}"

    assert checked >= 15, f"只解析出 {checked} 处调用，解析逻辑可能又退化了"
    assert not unresolved, "有调用无法解析出路径，需人工确认：" + "; ".join(unresolved)


def test_ui_exposes_all_four_knowledge_actions():
    """知识闭环的四个动作都必须有界面入口（阶段 11：全程不碰命令行）。

    少一个就意味着"某个动作只能靠 curl 调接口"——而验收原文要求的正是
    「全程不碰命令行」。这条同时也防住另一类退化：接口加了、页面忘了接。
    """
    text = (WEB_DIR / "templates" / "report.html").read_text(encoding="utf-8")
    knowledge = (WEB_DIR / "templates" / "knowledge.html").read_text(encoding="utf-8")
    both = text + knowledge

    for action in ("confirm", "false-positive", "reject"):
        assert f"/{action}" in both or f"act('{action}'" in both or f"'{action}'" in both, (
            f"界面没有提供 {action} 入口"
        )
    assert "/api/knowledge/candidates/${candidate.id}" in knowledge
    assert "API.patch(" in knowledge, "编辑标题（PATCH）没有界面入口"


def test_incomplete_and_failed_states_expose_a_retry_entry():
    r"""验收第 919 行：不完整 / 失败结果都要有显著提示**与重试入口**。

    实测原状态：失败态只有一个「回项目页，重新发起分析」的链接（用户得把
    时间窗重填一遍，填得不一样就不是同一次分析了），而 `partial_success`
    连入口都没有 —— 它恰恰是"结果不完整、最该重跑"的那一档。

    现在两处都直接调 `POST /api/runs/{id}/retry`：服务端按原 Run 的
    `input` 快照新建一个 Run，并用 `parent_run_id` 串起谱系。
    """
    status = (WEB_DIR / "templates" / "run_status.html").read_text(encoding="utf-8")
    report = (WEB_DIR / "templates" / "report.html").read_text(encoding="utf-8")

    for name, text in (("run_status.html", status), ("report.html", report)):
        assert "/retry" in text, f"{name} 没有「重新分析」入口"
        assert "partial_success" in text, f"{name} 未处理不完整态"
        assert "'failed'" in text or '"failed"' in text, f"{name} 未处理失败态"
        assert "不完整" in text, f"{name} 没有显著标注「结果不完整」"


def test_write_endpoints_are_either_used_by_the_ui_or_documented(routes):
    """每个写接口要么有界面入口，要么在这里写明"为什么不需要"。

    不写这条的话，"接口存在但没人能点到"会一直藏着；而一旦有人加了新接口，
    他又必须在这里交代一句 —— 这正是我们想要的摩擦。
    """
    ui_calls = {(m, p) for _, m, p, _ in _ui_calls()}
    documented_exceptions = {
        # 上传时会按「项目 + 格式」自动创建或复用 DataSource（计划第 522–525 行
        # 的简化口径：不要求用户先手工建 DataSource），故界面不需要这个入口。
        ("POST", "/api/projects/{project_id}/data-sources"),
        # 无界面入口的运维用途：手动改状态由 Worker/回收器负责。
        ("POST", "/api/runs/{run_id}/cancel"),
    }
    ui_calls |= documented_exceptions

    orphans = []
    for method, path in routes:
        if method == "GET":
            continue
        if any(m == method and _matches(call, path) for m, call in ui_calls):
            continue
        orphans.append(f"{method} {path}")

    assert not orphans, (
        "以下写接口既没有界面入口、也没在上面登记原因：\n  "
        + "\n  ".join(orphans)
    )
