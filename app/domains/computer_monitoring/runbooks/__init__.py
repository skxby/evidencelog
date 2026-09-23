"""Runbook 数据目录与加载入口。

YAML 与加载器共处一个目录（计划 §3.3 要求 `runbooks/` 是领域目录包的一部分），
故加载器实现在 `_loader.py`，本文件只做再导出，避免与数据文件命名冲突。
"""

from __future__ import annotations

from app.domains.computer_monitoring.runbooks._loader import (
    REQUIRED_KEYS,
    RunbookFormatError,
    load_runbooks,
    parse_runbook,
)

__all__ = ["REQUIRED_KEYS", "RunbookFormatError", "load_runbooks", "parse_runbook"]