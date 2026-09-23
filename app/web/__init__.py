"""前端：Jinja2 模板 + 原生 JS（**不引入前端构建工具**，计划第 899 行）。

页面只负责展示与调用 `/api/*`；**不承担分析逻辑**（那是 Worker 的事）。
鉴权靠登录时下发的 `httpOnly` Cookie —— 浏览器里的 JS 读不到它，
所以前端不需要（也不应该）自己保存 token。

本模块刻意**不 import 具体页面模块**，只提供模板环境与静态目录，
避免 `web.views` ←→ `web` 的循环导入。
"""

from __future__ import annotations

from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def get_templates():
    """构造 Jinja2 模板环境（延迟创建，避免 import 期依赖目录存在）。"""
    from fastapi.templating import Jinja2Templates

    return Jinja2Templates(directory=str(TEMPLATES_DIR))


def mount_static(app: object) -> None:
    """挂载静态资源目录。

    单独一个函数而不是在 import 时挂载：目录不存在时（例如只跑测试）
    不该让整个应用 import 失败。
    """
    if STATIC_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount(  # type: ignore[attr-defined]
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )


__all__ = ["STATIC_DIR", "TEMPLATES_DIR", "get_templates", "mount_static"]
