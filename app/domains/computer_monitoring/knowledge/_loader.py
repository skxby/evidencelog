"""领域知识加载与校验（YAML，V1 不建知识表）。

载体与目录（计划第 438–451 行、修订说明第 2 条）：

    seed 知识   → 领域目录包内的 `knowledge/confirmed/`   （代码资产，进 Git）
    运行时 confirmed → `${DATA_DIR}/knowledge/<domain>/confirmed/` （运行时数据，挂卷）
    运行时 staging   → `${DATA_DIR}/knowledge/<domain>/staging/candidates.yaml`

**不往代码目录写运行时数据**（修订说明第 2 条最后一句明确「这条不能破」）。

约束（计划第 491–496 行）由 `validate_entry` 强制：
1. draft 知识不参与自动结论；
2. 无 evidence、无 confidence 的条目**不允许**确认；
3. fix_suggestion 仅信息展示，不自动执行；
4. match 规则必须人工确认才生效 —— 故模型产出的候选一律 `status=draft`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.domains.protocol import (
    VALID_KNOWLEDGE_KINDS,
    KnowledgeEntry,
    KnowledgeNotConfirmableError,
)

#: 白名单的**真源在协议层**（`app/domains/protocol.py`）：核心 Runtime 也要按它
#: 归一化模型候选，不能各写一份。
VALID_KINDS = VALID_KNOWLEDGE_KINDS
VALID_STATUSES = ("draft", "confirmed", "deprecated")

logger = logging.getLogger(__name__)

#: V1 只支持这些固定操作符，**不做通用表达式引擎**（计划第 462、501 行）
ALLOWED_OPERATORS = (">", "<", "=", ">=", "<=", "!=")


class KnowledgeFormatError(ValueError):
    """知识条目结构不合法。"""


def validate_entry(data: dict[str, Any], *, source: str = "") -> KnowledgeEntry:
    """校验并构造一条知识。结构错误直接抛错，不静默跳过。"""
    where = f"{source}: " if source else ""
    if not isinstance(data, dict):
        raise KnowledgeFormatError(f"{where}条目必须是映射")

    for key in ("id", "kind", "title"):
        if not data.get(key):
            raise KnowledgeFormatError(f"{where}缺少必需字段 {key!r}")

    kind = str(data["kind"])
    if kind not in VALID_KINDS:
        raise KnowledgeFormatError(f"{where}kind={kind!r} 非法，允许 {VALID_KINDS}")

    status = str(data.get("status", "draft"))
    if status not in VALID_STATUSES:
        raise KnowledgeFormatError(f"{where}status={status!r} 非法，允许 {VALID_STATUSES}")

    confidence = data.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise KnowledgeFormatError(f"{where}confidence 必须在 0.0–1.0 之间")

    match = data.get("match") or {}
    if not isinstance(match, dict):
        raise KnowledgeFormatError(f"{where}match 必须是映射")
    condition = match.get("condition")
    if condition is not None:
        _validate_condition(str(condition), where=where)

    severity_hint = data.get("severity_hint")
    if severity_hint is not None and severity_hint not in ("high", "medium", "low"):
        raise KnowledgeFormatError(f"{where}severity_hint={severity_hint!r} 非法")

    entry = KnowledgeEntry(
        id=str(data["id"]),
        kind=kind,  # type: ignore[arg-type]
        title=str(data["title"]),
        match=dict(match),
        message_pattern=data.get("message_pattern"),
        severity_hint=severity_hint,
        description=str(data.get("description", "")),
        related=[str(r) for r in (data.get("related") or [])],
        evidence=dict(data.get("evidence") or {}),
        confidence=float(confidence),
        status=status,  # type: ignore[arg-type]
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )

    # 约束 2：confirmed 必须有 evidence 与 confidence
    if entry.status == "confirmed":
        assert_confirmable(entry)

    return entry


def _validate_condition(condition: str, *, where: str) -> None:
    """V1 只支持 `value <op> <number>` 这种固定形态，不做通用表达式引擎。"""
    parts = condition.split()
    if len(parts) != 3 or parts[0] != "value" or parts[1] not in ALLOWED_OPERATORS:
        raise KnowledgeFormatError(
            f"{where}condition={condition!r} 不被支持；"
            f"V1 只接受 'value <op> <number>'，操作符限 {ALLOWED_OPERATORS}"
        )
    try:
        float(parts[2])
    except ValueError:
        raise KnowledgeFormatError(f"{where}condition={condition!r} 的比较值不是数字") from None


def assert_confirmable(entry: KnowledgeEntry) -> None:
    """约束 2：无 evidence、无 confidence 的条目不允许确认（计划第 494 行）。"""
    event_ids = (entry.evidence or {}).get("event_ids") or []
    run_id = (entry.evidence or {}).get("run_id")
    if not event_ids and not run_id:
        raise KnowledgeNotConfirmableError(
            f"知识 {entry.id!r} 缺少 evidence（run_id / event_ids），不允许 confirmed"
        )
    if entry.confidence <= 0.0:
        raise KnowledgeNotConfirmableError(
            f"知识 {entry.id!r} 的 confidence 为 {entry.confidence}，不允许 confirmed"
        )


def load_entries_from_file(
    path: Path, *, tolerate_bad_entries: bool = False
) -> list[KnowledgeEntry]:
    """读一个 YAML 文件里的条目。

    `tolerate_bad_entries`：运行时文件（数据卷里的 confirmed / staging）用 True ——
    **逐条**跳过不合法的条目并指名告警，而不是一条坏条目带走整个文件。
    真机核验（2026-09-24）：模型自造 `kind=new_pattern` 的条目把同文件里
    人工确认过的那条也一起废掉了（整份文件抛错）。
    """
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    out: list[KnowledgeEntry] = []
    for entry in entries:
        try:
            out.append(validate_entry(entry, source=path.name))
        except (KnowledgeFormatError, KnowledgeNotConfirmableError) as exc:
            if not tolerate_bad_entries:
                raise
            logger.warning("知识条目被跳过（不合法）：%s（%s）", exc, path)
    return out


def load_confirmed(
    directory: Path, *, tolerate_bad_files: bool = False
) -> list[KnowledgeEntry]:
    """加载 `confirmed/` 下所有知识。

    `tolerate_bad_files` 区分**代码资产**与**运行时数据**：
      - seed（仓库内）保持严格：坏文件就是代码 bug，应当当场炸、由测试拦住；
      - 运行时 confirmed（数据卷里，可能被旧版本或手工编辑写坏）用 True：
        跳过坏文件并**指名告警**，不让一个坏文件把其余已确认知识一起带走。
        真机核验（2026-09-24）撞到的就是这个：一条模型自造 `kind` 的条目
        让整个 `knowledge()` 抛错，`confirmed_knowledge` 直接变 0。
    """
    if not directory.is_dir():
        return []
    out: list[KnowledgeEntry] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            out.extend(
                load_entries_from_file(path, tolerate_bad_entries=tolerate_bad_files)
            )
        except KnowledgeFormatError as exc:
            if not tolerate_bad_files:
                raise
            logger.warning("confirmed 知识文件被跳过（不合法）：%s（%s）", exc, path)
    return out


def load_staging(path: Path, *, tolerate_bad_entries: bool = False) -> list[KnowledgeEntry]:
    """加载 staging 候选。

    候选**一律是 draft**（约束 1/4）：即便文件里写了 confirmed，也在这里降级，
    因为候选来自模型生成、尚未经人工确认。

    `tolerate_bad_entries`：staging 是模型写的，运行时加载用 True ——
    逐条跳过不合法条目（告警指名），别让一条自造 kind 的候选把整份候选列表废掉。
    """
    entries = load_entries_from_file(path, tolerate_bad_entries=tolerate_bad_entries)
    downgraded: list[KnowledgeEntry] = []
    for entry in entries:
        if entry.status != "draft":
            entry = KnowledgeEntry(
                **{**entry.__dict__, "status": "draft"}
            )
        downgraded.append(entry)
    return downgraded


def load_all(domain_dir: Path, data_dir: Path | None = None, domain_id: str = "") -> dict:
    """按「先 seed，再叠加运行时 confirmed」的顺序加载（修订说明第 2 条）。

    seed 是代码资产；运行时 confirmed 覆盖同名 id。
    """
    seed = load_confirmed(domain_dir / "knowledge" / "confirmed")
    runtime: list[KnowledgeEntry] = []
    staging: list[KnowledgeEntry] = []

    if data_dir is not None and domain_id:
        base = Path(data_dir) / "knowledge" / domain_id
        # 运行时数据：坏文件跳过（告警指名），不连累其余知识
        runtime = load_confirmed(base / "confirmed", tolerate_bad_files=True)
        # staging 是**模型写的**，是这堆文件里唯一不受我们控制的一份。
        # 它坏掉只该让候选失效，不该把人工确认过的知识一起带走 ——
        # 真机核验（2026-09-24）：模型自造 `kind=new_pattern` 让 `knowledge()`
        # 整个抛错，`confirmed_knowledge` 直接变 0，闭环断在最后一步。
        # 仍然**不静默**：把文件名与原因打出来。
        staging_path = base / "staging" / "candidates.yaml"
        try:
            staging = load_staging(staging_path, tolerate_bad_entries=True)
        except KnowledgeFormatError as exc:
            # 领域包用 stdlib logging（不依赖 Runtime 的 structlog），
            # 所以消息自己拼好，别用 kwargs 结构化字段。
            logger.warning("staging 候选被忽略（文件不合法）：%s（%s）", exc, staging_path)
            staging = []

    merged: dict[str, KnowledgeEntry] = {e.id: e for e in seed}
    for entry in runtime:  # 运行时覆盖 seed
        merged[entry.id] = entry

    return {
        "confirmed": sorted(merged.values(), key=lambda e: e.id),
        "staging": staging,
    }
