"""Parser 层：BaseParser / TxtParser / JsonlParser。

与 Agent 逻辑解耦（计划第 527 行）：Parser 只认「文本 → 逻辑记录」，
字段抽取与语义交给 domain。
"""

from __future__ import annotations

from app.parsers.base import BaseParser, ParsedRecord, ParseStats
from app.parsers.jsonl import JsonlParser
from app.parsers.txt import TxtParser

__all__ = [
    "BaseParser",
    "JsonlParser",
    "ParseStats",
    "ParsedRecord",
    "TxtParser",
    "get_parser",
]


def get_parser(fmt: str) -> BaseParser:
    """按格式取 Parser。格式名不合法时抛错，**不默认回退**——
    静默回退会让 JSONL 被当 TXT 解析，产出一堆看似正常的坏结果。
    """
    normalized = fmt.strip().lower()
    if normalized in ("txt", "text", "log", "plain"):
        return TxtParser()
    if normalized in ("jsonl", "json", "ndjson"):
        return JsonlParser()
    raise ValueError(f"不支持的格式 {fmt!r}；V1 只支持 txt 与 jsonl")