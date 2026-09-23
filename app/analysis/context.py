"""Distilled Context：给模型的数据包（计划第 782–802 行）。

6 部分与 token 预算（计划第 795–799 行）：

| 等级 | 总预算 | Meta | Stats | Incidents | Anomalies | Samples | Context |
| L1   | 2000   | 100  | 200   | 300       | 400       | 700     | 100     |
| L2   | 4000   | 100  | 300   | 500       | 700       | 2100    | 300     |
| L3   | 8000   | 100  | 400   | 900       | 1300      | 4800    | 500     |

超长时按序压缩（计划第 801 行）：
降 Samples 采样 → 只留 top-k 异常 → 精简历史 Incident → 删低优先级 Context；
仍超限则**明确报错**「超出单次分析范围，请缩小时间范围」——
不静默截断成一份"看着正常"的 Context（红线 4）。
**自动分片聚合放 V1.5，V1 不做。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.gateways.base import TIER_L1, TIER_L2, TIER_L3

#: 计划第 795–799 行的预算表
CONTEXT_BUDGETS: dict[str, dict[str, int]] = {
    TIER_L1: {"total": 2000, "metadata": 100, "statistics": 200,
              "incidents": 300, "anomalies": 400, "samples": 700, "context": 100},
    TIER_L2: {"total": 4000, "metadata": 100, "statistics": 300,
              "incidents": 500, "anomalies": 700, "samples": 2100, "context": 300},
    TIER_L3: {"total": 8000, "metadata": 100, "statistics": 400,
              "incidents": 900, "anomalies": 1300, "samples": 4800, "context": 500},
}

#: 压缩顺序（计划第 801 行）
COMPRESSION_ORDER = ("samples", "anomalies", "incidents", "context")

#: 字符→token 的粗略换算。不引入 tokenizer 依赖：本函数只用于**预算控制**，
#: 真实用量以供应商返回的 usage 为准（阶段 06 已在记录）。
CHARS_PER_TOKEN = 3.0


class ContextBudgetExceededError(RuntimeError):
    """压缩到极限仍超预算 —— 明确报错，让用户缩小时间范围。

    计划第 802 行：**不自动分片**（那是 V1.5），所以这里必须失败而不是硬塞。
    """


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中文按 1 字 ≈ 1 token、英文按约 3 字符 ≈ 1 token 的混合经验值。
    只用 ASCII 时用 3 字符/token，否则按字符数（中文更密）。
    """
    if not text:
        return 0
    if text.isascii():
        return int(len(text) / CHARS_PER_TOKEN) + 1
    return len(text)


@dataclass
class ContextPart:
    """Context 的一部分。"""

    name: str
    budget: int
    payload: Any
    rendered: str = ""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.rendered)


@dataclass
class DistilledContext:
    """组装好的 Context。"""

    tier: str
    parts: dict[str, ContextPart] = field(default_factory=dict)
    #: 压缩过程中做过什么（可观测性）
    compression_notes: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(part.tokens for part in self.parts.values())

    @property
    def budget(self) -> int:
        return CONTEXT_BUDGETS[self.tier]["total"]

    def render(self) -> str:
        """拼成给模型的文本。"""
        blocks: list[str] = []
        for name in ("metadata", "statistics", "incidents", "anomalies", "samples", "context"):
            part = self.parts.get(name)
            if part is None or not part.rendered:
                continue
            blocks.append(f"## {name}\n{part.rendered}")
        return "\n\n".join(blocks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "total_tokens": self.total_tokens,
            "budget": self.budget,
            "by_part": {
                name: {"tokens": part.tokens, "budget": part.budget}
                for name, part in self.parts.items()
            },
            "compression_notes": list(self.compression_notes),
        }


def _render_samples(samples: list[dict[str, Any]]) -> str:
    lines = []
    for sample in samples:
        event_id = sample.get("event_id", "?")
        ts = sample.get("timestamp", "")
        severity = sample.get("severity", "")
        message = str(sample.get("message", ""))[:500]
        lines.append(f"[{event_id}] {ts} {severity} {message}")
    return "\n".join(lines)


def _render_anomalies(anomalies: list[dict[str, Any]]) -> str:
    lines = []
    for item in anomalies:
        lines.append(
            f"- {item.get('type', '?')} sev={item.get('severity', '?')} "
            f"event_ids={item.get('event_ids', [])} {item.get('message', '')}"[:400]
        )
    return "\n".join(lines)


def _render_incidents(incidents: list[dict[str, Any]]) -> str:
    lines = []
    for incident in incidents:
        lines.append(
            f"- {incident.get('title', '?')} sev={incident.get('severity', '?')} "
            f"groups={incident.get('group_ids', [])} "
            f"root_cause={incident.get('root_cause') or '（未判定）'}"[:400]
        )
    return "\n".join(lines)


def build_distilled_context(
    *,
    tier: str,
    metadata: dict[str, Any],
    statistics: dict[str, Any],
    incidents: list[dict[str, Any]] | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    samples: list[dict[str, Any]] | None = None,
    context_notes: list[str] | None = None,
    max_compression_rounds: int | None = None,
) -> DistilledContext:
    """按等级预算组装 Context，超长时按序压缩，仍超则明确报错。

    `max_compression_rounds` 用于**限制压缩轮数**：默认不限制（一直压到能进
    总预算）。把它设成小数可以让"压不动就报错"这条路径可被测试到——
    否则压缩总能成功，那段防御性代码就是无法验证的死分支。
    """
    if tier not in CONTEXT_BUDGETS:
        raise ValueError(f"未知等级 {tier!r}，预算表只覆盖 {sorted(CONTEXT_BUDGETS)}")

    import json

    budgets = CONTEXT_BUDGETS[tier]
    samples = list(samples or [])
    anomalies = list(anomalies or [])
    incidents = list(incidents or [])
    context_notes = list(context_notes or [])

    def assemble(
        *, sample_rows: list[dict], anomaly_rows: list[dict],
        incident_rows: list[dict], note_rows: list[str],
    ) -> dict[str, ContextPart]:
        return {
            "metadata": ContextPart("metadata", budgets["metadata"],
                                    metadata, json.dumps(metadata, ensure_ascii=False, default=str)),
            "statistics": ContextPart("statistics", budgets["statistics"],
                                      statistics, json.dumps(statistics, ensure_ascii=False, default=str)),
            "incidents": ContextPart("incidents", budgets["incidents"],
                                     incident_rows, _render_incidents(incident_rows)),
            "anomalies": ContextPart("anomalies", budgets["anomalies"],
                                     anomaly_rows, _render_anomalies(anomaly_rows)),
            "samples": ContextPart("samples", budgets["samples"],
                                   sample_rows, _render_samples(sample_rows)),
            "context": ContextPart("context", budgets["context"],
                                   note_rows, "\n".join(note_rows)),
        }

    parts = assemble(
        sample_rows=samples, anomaly_rows=anomalies,
        incident_rows=incidents, note_rows=context_notes,
    )
    result = DistilledContext(tier=tier, parts=parts)

    # ---- 按序压缩（计划第 801 行）----
    # 刻意**先不做**「各部分压到自身预算」这一步：那会让每个部分各自合规、
    # 总和必然 ≤ 总预算，于是最后的超限报错变成永远走不到的死代码，
    # 而且高优先级部分（如 anomalies）明明可以借用低优先级部分（如 samples）
    # 的余量，却会被自己的预算硬切。
    # 这里改为：只按计划的优先级顺序压缩，直到**总预算**满足；压不动才报错。
    if result.total_tokens > budgets["total"]:
        result.compression_notes.append(
            f"初始 {result.total_tokens} tokens 超出总预算 {budgets['total']}，开始按序压缩"
        )
    _compress_to_total_budget(result, budgets["total"], max_rounds=max_compression_rounds)

    if result.total_tokens > budgets["total"]:
        raise ContextBudgetExceededError(
            f"压缩到极限仍超出单次分析范围（{result.total_tokens} > "
            f"{budgets['total']} tokens，等级 {tier}）。请缩小时间范围后重试。"
            "（V1 不做自动分片聚合）"
        )

    return result


#: 各部分的压缩优先级（数字越小越先被压缩）——与计划的压缩顺序一致
_COMPRESSION_PRIORITY = {"samples": 0, "anomalies": 1, "incidents": 2, "context": 3}

#: 单行渲染的部分（无法按行截断，只能整段丢弃）
_SINGLE_LINE_PARTS = ("metadata", "statistics")


def _compress_to_total_budget(
    result: DistilledContext, total_budget: int, *, max_rounds: int | None = None
) -> None:
    """按优先级压缩，直到总预算满足或再也压不动。

    `max_rounds` 限制总压缩轮数，用于让"压不动"的分支可测。
    """
    rounds = 0

    def out_of_rounds() -> bool:
        return max_rounds is not None and rounds >= max_rounds

    # ① 单行部分若单条就超预算，只能整段丢弃（并留痕）
    for name in _SINGLE_LINE_PARTS:
        if result.total_tokens <= total_budget or out_of_rounds():
            return
        part = result.parts[name]
        if part.tokens > part.budget and part.rendered:
            original = part.tokens
            part.payload = {}
            part.rendered = ""
            rounds += 1
            result.compression_notes.append(
                f"{name} 单行内容 {original} tokens 超出其预算 {part.budget}，"
                "为满足总预算已整段丢弃 —— 该部分信息缺失"
            )

    # ② 多行部分按行截断到自身预算
    for name in ("incidents", "anomalies", "samples", "context"):
        if result.total_tokens <= total_budget or out_of_rounds():
            return
        part = result.parts[name]
        if part.tokens <= part.budget:
            continue
        _truncate_lines(part, part.budget, result.compression_notes)
        rounds += 1

    # ③ 仍超则按计划顺序逐级减半/清空
    for name in COMPRESSION_ORDER:
        while result.total_tokens > total_budget:
            if out_of_rounds():
                return
            part = result.parts[name]
            before = part.tokens
            _reduce_part(part, result.compression_notes)
            rounds += 1
            if part.tokens >= before:
                break  # 这一部分已经压不动了，交给下一个优先级


def _truncate_lines(part: ContextPart, budget: int, notes: list[str]) -> None:
    if part.tokens <= budget:
        return
    lines = part.rendered.split("\n")
    original = part.tokens
    while lines and estimate_tokens("\n".join(lines)) > budget:
        lines.pop()
    part.rendered = "\n".join(lines)
    if not part.rendered.strip():
        notes.append(f"{part.name} 无法按行截断到 {budget} tokens（原 {original}），已整段丢弃")
    else:
        notes.append(f"{part.name} 按预算截断至 {part.tokens} tokens")


def _reduce_part(part: ContextPart, notes: list[str]) -> None:
    """把一个部分减半或清空（降低优先级最低的信息密度）。"""
    current = list(part.payload) if isinstance(part.payload, list) else None

    if part.name == "samples":
        if not current:
            return
        if len(current) <= 1:
            part.payload, part.rendered = [], ""
            notes.append("samples 已清空")
            return
        kept = current[: max(1, len(current) // 2)]
        part.payload = kept
        part.rendered = _render_samples(kept)
    elif part.name == "anomalies":
        if not current:
            return
        if len(current) <= 1:
            part.payload, part.rendered = [], ""
            notes.append("anomalies 已清空")
            return
        ordered = sorted(
            current,
            key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a.get("severity", "low"), 3),
        )
        kept = ordered[: max(1, len(ordered) // 2)]
        part.payload = kept
        part.rendered = _render_anomalies(kept)
    elif part.name == "incidents":
        if not current:
            return
        if len(current) <= 1:
            part.payload, part.rendered = [], ""
            notes.append("incidents 已清空")
            return
        kept = current[: max(1, len(current) // 2)]
        part.payload = kept
        part.rendered = _render_incidents(kept)
    else:  # context
        if not part.rendered:
            return
        part.payload, part.rendered = [], ""
        notes.append("context 已删除（最低优先级）")
