"""领域知识：数据目录（confirmed / staging）与加载入口。

知识是**代码资产还是运行时数据**，两者位置不同（修订说明第 2 条）：

    seed 知识（手写、进 Git）        -> 本目录下 confirmed/
    运行时 confirmed（人工确认生效）  -> ${DATA_DIR}/knowledge/<domain>/confirmed/
    运行时 staging（模型候选）        -> ${DATA_DIR}/knowledge/<domain>/staging/candidates.yaml

**不往代码目录写运行时数据。** 加载顺序是「先 seed，再叠加运行时 confirmed」。

加载器实现在 `_loader.py`，本文件只做再导出。
"""

from __future__ import annotations

from app.domains.computer_monitoring.knowledge._loader import (
    ALLOWED_OPERATORS,
    VALID_KINDS,
    VALID_STATUSES,
    KnowledgeFormatError,
    assert_confirmable,
    load_all,
    load_confirmed,
    load_entries_from_file,
    load_staging,
    validate_entry,
)

__all__ = [
    "ALLOWED_OPERATORS",
    "VALID_KINDS",
    "VALID_STATUSES",
    "KnowledgeFormatError",
    "assert_confirmable",
    "load_all",
    "load_confirmed",
    "load_entries_from_file",
    "load_staging",
    "validate_entry",
]