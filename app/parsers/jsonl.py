"""JsonlParser —— 每行一个 JSON 对象。

计划第 531 行：**坏行计数并报告，不让单行坏数据拖垮整文件**。
所以坏 JSON 只记坏行、继续解析后续行，绝不整文件失败。

JSONL 的字段名是任意的（真实 nginx JSON 用 `time` / `remote_ip` / `request`），
故时间戳从若干候选键里取；仍取不到就算坏行，**不用 now() 兜底**（红线 4）。
"""

from __future__ import annotations

import json
from typing import Any

from app.parsers.base import BaseParser, ParsedRecord

#: 时间戳候选键。顺序即优先级。
TIMESTAMP_KEYS = ("timestamp", "ts", "@timestamp", "time", "datetime", "date")

#: 严重度候选键
SEVERITY_KEYS = ("severity", "level", "loglevel", "log_level")

#: 消息候选键
MESSAGE_KEYS = ("message", "msg", "log", "event", "request", "text")


class JsonlParser(BaseParser):
    """逐行 JSON 解析。"""

    name = "jsonl"

    def __init__(self, *, require_object: bool = True) -> None:
        super().__init__()
        #: 每行必须是 JSON **对象**；数组/标量视为坏行（JSONL 语义如此）
        self.require_object = require_object
        #: 无法解析的行（JSON 坏 或 缺时间戳）分别计数，便于定位是格式问题还是数据问题
        self.invalid_json = 0
        self.missing_timestamp = 0

    def parse(self, text: str, domain: Any) -> list[ParsedRecord]:
        self.reset()
        self.invalid_json = 0
        self.missing_timestamp = 0
        records: list[ParsedRecord] = []

        for index, line in enumerate(text.splitlines(), start=1):
            self.stats.total_lines += 1
            if not line.strip():
                self.stats.skipped += 1
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self.invalid_json += 1
                self.stats.record_bad(line)
                continue

            if self.require_object and not isinstance(payload, dict):
                self.invalid_json += 1
                self.stats.record_bad(line)
                continue

            record = self._build_record(payload, line, index, domain)
            if record is not None:
                records.append(record)

        return records

    def _build_record(
        self, payload: dict[str, Any], line: str, line_number: int, domain: Any
    ) -> ParsedRecord | None:
        raw_fields = self._extract_fields(payload)
        if raw_fields["ts"] is None:
            # 缺时间戳：计入坏行并留证，绝不补 now()
            self.missing_timestamp += 1
            self.stats.record_bad(line)
            return None

        try:
            event = domain.normalize(raw_fields)
        except Exception as exc:  # noqa: BLE001
            self.stats.record_bad(f"{line[:120]}  <normalize failed: {type(exc).__name__}: {exc}>")
            return None

        self.stats.parsed += 1
        return ParsedRecord(
            line_number=line_number,
            raw_text=line,
            event=event,
            raw_fields=raw_fields,
        )

    def _extract_fields(self, payload: dict[str, Any]) -> dict[str, Any]:
        """从任意 JSON 对象里抽出统一字段。

        取不到就留 None —— 由调用方决定算不算坏行，不在这里瞎猜。
        """
        ts = self._first_key(payload, TIMESTAMP_KEYS)
        severity = self._first_key(payload, SEVERITY_KEYS)
        message = self._first_key(payload, MESSAGE_KEYS)

        # 指标事件的显式声明（`metric_name` + `value`）优先于猜测
        metric_name = payload.get("metric_name")
        value = payload.get("value")

        fields: dict[str, Any] = {
            "ts": ts,
            "msg": "" if message is None else str(message),
            "format": "jsonl",
            "raw_json": dict(payload),
        }
        if severity is not None:
            fields["level"] = str(severity).upper()
        if metric_name is not None and isinstance(value, (int, float)):
            fields["metric_name"] = str(metric_name)
            fields["value"] = float(value)
            if payload.get("unit") is not None:
                fields["unit"] = str(payload["unit"])
        return fields

    @staticmethod
    def _first_key(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        return None

    def as_dict(self) -> dict[str, Any]:
        base = super().as_dict()
        base["invalid_json"] = self.invalid_json
        base["missing_timestamp"] = self.missing_timestamp
        return base
