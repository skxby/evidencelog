"""TxtParser —— 按行解析 TXT 日志。

处理三件事（计划第 530 行）：
1. **多行堆栈合并**：续行（以空白开头，或以 `at ` / `Caused by:` / `...` 开头）
   并入上一条逻辑记录。不合并的话，Java/Python 堆栈会被算成一堆坏行。
2. 空行 / 注释行：单独计 `skipped`，不污染坏行率。
3. 坏行：**计数并保留样本**，绝不静默丢弃。
"""

from __future__ import annotations

from typing import Any

from app.parsers.base import BaseParser, ParsedRecord

#: 把一行判定为「续行」的形态
_CONTINUATION_PREFIXES = (
    "at ",
    "in ",
    "Caused by:",
    "...",
    "Traceback",
    "\t",
)


def is_continuation(line: str) -> bool:
    """判断是否为续行（属于上一条逻辑记录）。

    以空白开头是最强的信号（堆栈缩进）；也认几个常见的关键词开头。
    """
    if not line:
        return False
    if line[0] in (" ", "\t"):
        return True
    return any(line.startswith(prefix) for prefix in _CONTINUATION_PREFIXES)


def is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


class TxtParser(BaseParser):
    """按行解析，带多行堆栈合并。"""

    name = "txt"

    def __init__(self, *, max_continuation_lines: int = 500) -> None:
        super().__init__()
        # 防御恶意/损坏输入：无限续行会把整份文件并成一条记录
        self.max_continuation_lines = max_continuation_lines

    def parse(self, text: str, domain: Any) -> list[ParsedRecord]:
        self.reset()
        records: list[ParsedRecord] = []

        # 待处理的逻辑记录
        buffer: list[str] = []
        buffer_start_line = 0

        def flush() -> None:
            if not buffer:
                return
            merged_text = "\n".join(buffer)
            record = self._build_record(merged_text, buffer_start_line, domain)
            if record is not None:
                records.append(record)
            buffer.clear()

        for index, line in enumerate(text.splitlines(), start=1):
            self.stats.total_lines += 1

            if is_blank_or_comment(line):
                self.stats.skipped += 1
                continue

            if is_continuation(line) and buffer:
                if len(buffer) < self.max_continuation_lines:
                    buffer.append(line)
                    self.stats.merged += 1
                else:
                    # 超过上限：如实记为坏行，而不是无限吞
                    self.stats.record_bad(line)
                continue

            flush()
            buffer = [line]
            buffer_start_line = index

        flush()
        return records

    def _build_record(self, text: str, line_number: int, domain: Any) -> ParsedRecord | None:
        """把一条逻辑记录交给 domain 解析；失败则计入坏行。

        多行记录的处理方式：**用首行抽结构化字段**（时间戳/进程等只出现在首行），
        再把 `msg` 换成**完整的合并文本**——堆栈内容全在续行里，
        若只用首行的 msg 会把它整段丢掉（那才是真正的信息损失）。
        """
        is_multiline = "\n" in text
        first_line = text.split("\n", 1)[0]

        raw_fields = domain.parse_line(first_line)
        if raw_fields is None:
            # 首行都解析不了：整条记为坏行，保留全文样本便于排障
            self.stats.record_bad(text)
            return None

        if is_multiline:
            raw_fields = {**raw_fields, "msg": text}

        try:
            event = domain.normalize(raw_fields)
        except Exception as exc:  # noqa: BLE001 - 归一化失败要计入坏行并留证
            self.stats.record_bad(f"{text[:120]}  <normalize failed: {type(exc).__name__}: {exc}>")
            return None

        self.stats.parsed += 1
        return ParsedRecord(
            line_number=line_number,
            raw_text=text,
            event=event,
            raw_fields=raw_fields,
        )
