"""Runbook 加载（YAML → `Runbook`）。

计划第 412 行：runbook 是**可执行 procedure**，不是知识库文档。
计划第 495 行：V1 只读展示，**不自动执行**（与只读定位一致）。

任何结构错误都直接抛异常并指出是哪个文件——静默跳过会让「runbook 缺失」
在运行期才暴露，那正是红线 4 要防的。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.domains.protocol import Runbook

REQUIRED_KEYS = ("id", "title", "applies", "steps")


class RunbookFormatError(ValueError):
    """runbook YAML 结构不合法。"""


def parse_runbook(data: dict, *, source: str) -> Runbook:
    if not isinstance(data, dict):
        raise RunbookFormatError(f"{source}: 顶层必须是映射（mapping）")

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise RunbookFormatError(f"{source}: 缺少必需字段 {missing}")

    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise RunbookFormatError(f"{source}: steps 必须是非空列表")

    applies = data["applies"]
    if not isinstance(applies, dict):
        raise RunbookFormatError(f"{source}: applies 必须是映射")

    references = data.get("references") or []
    if not isinstance(references, list):
        raise RunbookFormatError(f"{source}: references 必须是列表")

    return Runbook(
        id=str(data["id"]),
        title=str(data["title"]),
        applies=dict(applies),
        steps=[str(s) for s in steps],
        references=[str(r) for r in references],
    )


def load_runbooks(directory: Path) -> list[Runbook]:
    """加载目录下所有 `*.yaml` / `*.yml` runbook，按 id 排序。

    目录不存在时返回空列表（尚未写 runbook 是合法状态），但**单个文件坏掉会抛错**。
    """
    if not directory.is_dir():
        return []

    runbooks: list[Runbook] = []
    seen_ids: dict[str, str] = {}
    for path in sorted(directory.glob("*.y*ml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            raise RunbookFormatError(f"{path.name}: 文件为空")
        # 一个文件一个 runbook；写成列表则逐个解析
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            runbook = parse_runbook(entry, source=path.name)
            if runbook.id in seen_ids:
                raise RunbookFormatError(
                    f"{path.name}: runbook id {runbook.id!r} 与 {seen_ids[runbook.id]} 重复"
                )
            seen_ids[runbook.id] = path.name
            runbooks.append(runbook)

    return sorted(runbooks, key=lambda r: r.id)
