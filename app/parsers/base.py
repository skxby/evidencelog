"""Parser 基类与解析统计（计划第 527–532 行）。

```text
BaseParser ├── TxtParser   （按行；处理多行堆栈的合并、空行/截断行）
           └── JsonlParser （每行一个 JSON；坏行计数并报告）
```

**职责边界（重要）**：Parser 只管**结构**（哪几行构成一条逻辑记录、哪行是坏行、
时间戳字符串长什么样）。字段抽取与语义由 **domain** 负责（`DomainBase.parse_line`）。
这样同一个 Parser 能服务不同领域，而领域换了 Parser 也不用改。

**不做归零**：坏行只计数并**保留样本**，绝不静默丢弃（红线 4）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: 单个坏行样本保留的最大长度，避免错误报告本身撑爆内存
BAD_LINE_SAMPLE_LIMIT = 200
#: 最多保留多少个坏行样本
BAD_LINE_SAMPLE_COUNT = 20


@dataclass
class ParseStats:
    """解析统计。计划第 519 行要求返回「总行 / 成功 / 坏行 / 脱敏计数」。"""

    total_lines: int = 0
    parsed: int = 0
    bad_lines: int = 0
    #: 空行 / 注释行：既不是成功也不是坏行，单独计数以免污染坏行率
    skipped: int = 0
    #: 被合并进上一条逻辑记录的行数（多行堆栈）
    merged: int = 0
    bad_line_samples: list[str] = field(default_factory=list)

    def record_bad(self, line: str) -> None:
        self.bad_lines += 1
        if len(self.bad_line_samples) < BAD_LINE_SAMPLE_COUNT:
            self.bad_line_samples.append(line[:BAD_LINE_SAMPLE_LIMIT])

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_lines": self.total_lines,
            "parsed": self.parsed,
            "bad_lines": self.bad_lines,
            "skipped": self.skipped,
            "merged": self.merged,
            # 坏行率以「参与解析的行」为分母，空行不该拉低它
            "bad_line_rate": (
                round(self.bad_lines / (self.parsed + self.bad_lines), 4)
                if (self.parsed + self.bad_lines)
                else 0.0
            ),
            "bad_line_samples": list(self.bad_line_samples),
        }


@dataclass
class ParsedRecord:
    """一条逻辑记录：原始文本 + 归一化后的标准事件。"""

    line_number: int
    raw_text: str
    event: Any  # NormalizedEvent；用 Any 以免 parsers 依赖 domain 层类型
    #: domain 抽出的原始字段，排障时有用
    raw_fields: dict[str, Any] = field(default_factory=dict)


class BaseParser(ABC):
    """Parser 契约。"""

    #: 供日志与报告显示
    name: str

    def __init__(self) -> None:
        self.stats = ParseStats()

    @abstractmethod
    def parse(self, text: str, domain: Any) -> list[ParsedRecord]:
        """把脱敏后的文本解析成逻辑记录列表。

        `domain` 必须实现 `DomainBase.parse_line` 与 `DomainBase.normalize`。
        """

    def reset(self) -> None:
        self.stats = ParseStats()

    def as_dict(self) -> dict[str, Any]:
        return {"parser": self.name, **self.stats.as_dict()}
