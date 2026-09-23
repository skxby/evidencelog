"""Golden Set：固定数据集 + 期望结论（计划第 952-965 行）。

用法：
    pytest tests/golden -q            # 跑 5 个场景并逐条判定
    python -m tests.golden.runner     # 打印表格与成本
"""

from __future__ import annotations

from tests.golden.expectations import (
    SCENARIOS,
    Expectation,
    ExpectationFormatError,
    load_all_expectations,
    load_expectation,
    log_files,
)
from tests.golden.runner import (
    CheckResult,
    GoldenReport,
    ScenarioOutcome,
    check_expectation,
    run_all,
    run_scenario,
)

__all__ = [
    "SCENARIOS",
    "CheckResult",
    "Expectation",
    "ExpectationFormatError",
    "GoldenReport",
    "ScenarioOutcome",
    "check_expectation",
    "load_all_expectations",
    "load_expectation",
    "log_files",
    "run_all",
    "run_scenario",
]
