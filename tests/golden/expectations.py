"""Golden Set 场景断言（计划第 952–965 行）。

## 断言从哪里来

**不是我编的。** `expect.json` 里的每一条都对应《V1 编码计划》阶段 13 写明的
期望，或计划其他章节的明确规定：

| 场景 | 计划原文 | 出处 |
|---|---|---|
| normal | 正常日志，期望 L0、无异常 Insight | 第 956 行 |
| cpu_anomaly | 期望命中 CPU 异常 | 第 957 行 |
| memory_growth | 期望命中内存持续增长 | 第 958 行 |
| crash | 期望命中进程崩溃 | 第 959 行 |
| malformed | 坏格式，期望**明确失败而非假报告** | 第 960 行 + 红线 4 |

标注权在用户：`expect.json` 是可编辑的纯 JSON，改它不需要动代码。
`source` 字段记录每条期望的依据，便于你复核我有没有曲解。

## 为什么用 JSON 而不是代码

计划第 965 行「V1 用人工核对 Golden Set 结果即可」。把期望放在数据文件里，
你改期望时不必碰 Python，也避免"期望"与"实现"耦合在同一语言里互相迁就。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASETS_DIR = Path(__file__).resolve().parents[1] / "datasets"

#: 5 个固定场景（计划第 955–960 行）
SCENARIOS = ("normal", "cpu_anomaly", "memory_growth", "crash", "malformed")


class ExpectationFormatError(ValueError):
    """expect.json 结构不合法 —— 明确报错，不跳过该场景。"""


@dataclass
class Expectation:
    """一个场景的期望。"""

    scenario: str
    description: str = ""
    #: 期望的复杂度等级；None 表示不约束
    tier: str | None = None
    #: 期望命中的 analyzer 名（必须全部命中）
    expect_anomaly_types: list[str] = field(default_factory=list)
    #: 期望没有任何异常
    expect_no_anomalies: bool = False
    #: 解析统计的下限/上限
    min_parsed: int | None = None
    max_parsed: int | None = None
    min_bad_lines: int | None = None
    #: 要求"失败要明确"（坏格式场景）
    expect_explicit_failure: bool = False
    #: 每条期望的依据（计划出处），便于人工复核
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "description": self.description,
            "tier": self.tier,
            "expect_anomaly_types": list(self.expect_anomaly_types),
            "expect_no_anomalies": self.expect_no_anomalies,
            "min_parsed": self.min_parsed,
            "max_parsed": self.max_parsed,
            "min_bad_lines": self.min_bad_lines,
            "expect_explicit_failure": self.expect_explicit_failure,
            "sources": dict(self.sources),
        }


def scenario_dir(scenario: str, *, datasets_dir: Path | None = None) -> Path:
    return (datasets_dir or DATASETS_DIR) / scenario


def log_files(scenario: str, *, datasets_dir: Path | None = None) -> list[Path]:
    """取该场景下的日志样本（按名排序，保证顺序确定）。"""
    directory = scenario_dir(scenario, datasets_dir=datasets_dir)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.name != "expect.json"
    )


def load_expectation(
    scenario: str, *, datasets_dir: Path | None = None
) -> Expectation:
    """读取并校验 `expect.json`。

    **缺文件或结构非法一律抛错**，不静默跳过：一个"被跳过的场景"在测试报告里
    看起来和一个"通过的场景"几乎一样，那正是红线 4 要防的。
    """
    path = scenario_dir(scenario, datasets_dir=datasets_dir) / "expect.json"
    if not path.is_file():
        raise ExpectationFormatError(
            f"场景 {scenario} 缺少 expect.json（{path}）；"
            "期望必须显式声明，不允许默认通过"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExpectationFormatError(f"场景 {scenario} 的 expect.json 不是合法 JSON：{exc}") from exc

    if not isinstance(raw, dict):
        raise ExpectationFormatError(f"场景 {scenario} 的 expect.json 顶层必须是对象")

    known = set(Expectation.__dataclass_fields__) - {"scenario"}
    unknown = set(raw) - known
    if unknown:
        # 未知键通常意味着拼错，静默忽略会让"我明明写了期望"却完全不生效
        raise ExpectationFormatError(
            f"场景 {scenario} 的 expect.json 含未知字段 {sorted(unknown)}；"
            f"可用字段：{sorted(known)}"
        )

    tier = raw.get("tier")
    if tier is not None and tier not in ("L0", "L1", "L2", "L3"):
        raise ExpectationFormatError(f"场景 {scenario} 的 tier={tier!r} 非法")

    return Expectation(
        scenario=scenario,
        description=str(raw.get("description", "")),
        tier=tier,
        expect_anomaly_types=[str(t) for t in (raw.get("expect_anomaly_types") or [])],
        expect_no_anomalies=bool(raw.get("expect_no_anomalies", False)),
        min_parsed=raw.get("min_parsed"),
        max_parsed=raw.get("max_parsed"),
        min_bad_lines=raw.get("min_bad_lines"),
        expect_explicit_failure=bool(raw.get("expect_explicit_failure", False)),
        sources=dict(raw.get("sources") or {}),
    )


def load_all_expectations(*, datasets_dir: Path | None = None) -> dict[str, Expectation]:
    return {
        scenario: load_expectation(scenario, datasets_dir=datasets_dir)
        for scenario in SCENARIOS
    }
