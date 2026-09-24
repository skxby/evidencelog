"""Golden Set 运行器（计划第 952–965 行）。

跑法：每个场景的样本走**真实上传管道**（阶段 04：脱敏 → 落盘 → 解析 → 入库），
再跑**真实分析链路**（阶段 09），最后对照 `expect.json` 逐条判定。

刻意不用任何测试专用旁路：
- 不走"直接构造 Event 对象"，而是走 UploadService，这样解析/脱敏/时间戳归一
  都真的被执行；
- 分析走 `analyze()`，与 Worker 调的是同一个函数。

每次运行记录成本（计划第 964 行：成本较历史明显异常时人工排查）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.analysis.pipeline import PipelineInput, analyze
from app.domains.wiring import build_default_registry
from app.services.upload_service import UploadService
from tests.golden.expectations import (
    SCENARIOS,
    Expectation,
    load_expectation,
    log_files,
)

#: 与 `.env` 的 DEFAULT_TIMEZONE 一致；Golden Set 用固定值保证可复现
GOLDEN_TIMEZONE = "Asia/Shanghai"


@dataclass
class ScenarioOutcome:
    """一个场景的实际结果。"""

    scenario: str
    parsed: int = 0
    bad_lines: int = 0
    events_persisted: int = 0
    tier: str = ""
    anomaly_types: list[str] = field(default_factory=list)
    insight_count: int = 0
    #: 成本（元）。L0 场景应为 0（不调用模型）
    cost: float = 0.0
    facts_without_evidence: int = 0
    rules_only: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "parsed": self.parsed,
            "bad_lines": self.bad_lines,
            "events_persisted": self.events_persisted,
            "tier": self.tier,
            "anomaly_types": self.anomaly_types,
            "insight_count": self.insight_count,
            "cost": round(self.cost, 6),
            "facts_without_evidence": self.facts_without_evidence,
            "rules_only": self.rules_only,
            "notes": self.notes,
        }


@dataclass
class CheckResult:
    """一条期望的判定。"""

    scenario: str
    assertion: str
    passed: bool
    detail: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "assertion": self.assertion,
            "passed": self.passed,
            "detail": self.detail,
            "source": self.source,
        }


def run_scenario(
    scenario: str,
    *,
    session: Session,
    project_id: int,
    uploads_dir: Path,
    router: Any = None,
    domain: Any = None,
) -> tuple[ScenarioOutcome, Any]:
    """跑一个场景，返回 (实际结果, PipelineResult)。

    `router=None` 时只跑确定性部分（模型不可用的降级路径）——
    Golden Set 的"关键 Insight 命中"判定不依赖模型，故默认离线可跑。
    """
    domain = domain or build_default_registry().load("computer_monitoring")
    outcome = ScenarioOutcome(scenario=scenario)

    files = log_files(scenario)
    if not files:
        outcome.notes.append(f"场景 {scenario} 没有样本文件")
        return outcome, None

    pipeline_events: list[dict[str, Any]] = []
    service = UploadService(
        session,
        uploads_dir=uploads_dir,
        domain_registry=None,
        default_timezone=GOLDEN_TIMEZONE,
    )

    for path in files:
        fmt = "jsonl" if path.suffix.lower() in (".jsonl", ".json") else "txt"
        result = service.ingest(
            project_id=project_id,
            content=path.read_bytes(),
            filename=path.name,
            fmt=fmt,
        )
        stats = result.parse_stats
        outcome.parsed += int(stats.get("parsed", 0))
        outcome.bad_lines += int(stats.get("bad_lines", 0))
        outcome.events_persisted += int(result.events_persisted)

    # 从库里取回事件，喂给分析链路（与 Worker **同一个**装载器）
    from app.repositories.event import EventRepository

    pipeline_events.extend(EventRepository(session).pipeline_events(project_id))

    if not pipeline_events:
        # 一个事件都没有也能判定（malformed 场景就是靠这个）
        outcome.notes.append("没有事件进入分析链路")
        return outcome, None

    timestamps = [e["timestamp"] for e in pipeline_events]
    result = analyze(
        PipelineInput(
            project_id=project_id,
            source_id=pipeline_events[0]["source_id"],
            domain_id=domain.domain_id,
            domain_version=domain.version,
            time_start=min(timestamps),
            time_end=max(timestamps),
            events=pipeline_events,
        ),
        analyzers=domain.analyzers(),
        domain=domain,
        router=router,
    )

    outcome.tier = result.tier
    outcome.anomaly_types = sorted({str(a.get("type")) for a in result.anomalies})
    outcome.insight_count = len(result.insights)
    outcome.cost = sum(float(a.get("cost") or 0) for a in result.model_attempts)
    outcome.rules_only = result.used_rules_only
    # 红线 3 的独立检查：fact 必须有证据
    outcome.facts_without_evidence = sum(
        1 for i in result.insights if i.type == "fact" and not i.evidence_ids
    )
    outcome.notes.extend(result.notes)
    return outcome, result


def check_expectation(
    expectation: Expectation, outcome: ScenarioOutcome, result: Any
) -> list[CheckResult]:
    """逐条对照期望。**每条判定都带上依据**，便于人工复核。"""
    checks: list[CheckResult] = []
    scenario = expectation.scenario

    def add(
        assertion: str, passed: bool, detail: str = "", source_key: str = ""
    ) -> None:
        checks.append(
            CheckResult(
                scenario=scenario,
                assertion=assertion,
                passed=passed,
                detail=detail,
                source=expectation.sources.get(source_key, ""),
            )
        )

    # ---- 解析统计 ----
    if expectation.min_parsed is not None:
        add(
            f"成功解析 ≥ {expectation.min_parsed} 行",
            outcome.parsed >= expectation.min_parsed,
            f"实际 {outcome.parsed}",
            "min_parsed",
        )
    if expectation.max_parsed is not None:
        add(
            f"成功解析 ≤ {expectation.max_parsed} 行",
            outcome.parsed <= expectation.max_parsed,
            f"实际 {outcome.parsed}",
        )
    if expectation.min_bad_lines is not None:
        add(
            f"坏行 ≥ {expectation.min_bad_lines}（坏行必须被计数而不是静默丢弃）",
            outcome.bad_lines >= expectation.min_bad_lines,
            f"实际 {outcome.bad_lines}",
            "failure" if scenario == "malformed" else "",
        )

    # ---- 坏格式必须"明确失败而非假报告" ----
    if expectation.expect_explicit_failure:
        # "假报告"的定义：一行都没解析成功，却没报任何坏行
        explicit = outcome.bad_lines > 0
        add(
            "坏格式被明确指出（坏行计数 > 0）",
            explicit,
            f"坏行 {outcome.bad_lines}，成功 {outcome.parsed}",
            "failure",
        )
        # 单行坏数据不该拖垮整文件（计划第 531 行）
        if expectation.min_parsed:
            add(
                "合法行仍然正常入库（单行坏数据没拖垮整文件）",
                outcome.parsed >= expectation.min_parsed,
                f"实际 {outcome.parsed}",
                "min_parsed",
            )

    # ---- 等级 ----
    if expectation.tier is not None:
        add(
            f"复杂度等级为 {expectation.tier}",
            outcome.tier == expectation.tier,
            f"实际 {outcome.tier}",
            "tier",
        )

    # ---- 异常命中 ----
    if expectation.expect_no_anomalies:
        add(
            "没有任何异常命中",
            not outcome.anomaly_types,
            f"实际命中 {outcome.anomaly_types}",
            "tier",
        )
    for expected_type in expectation.expect_anomaly_types:
        add(
            f"命中 {expected_type}",
            expected_type in outcome.anomaly_types,
            f"实际命中 {outcome.anomaly_types}",
            "anomaly",
        )

    # ---- 红线 3：fact 必须有证据（对每个场景都成立）----
    add(
        "所有 fact 都有有效证据（红线 3）",
        outcome.facts_without_evidence == 0,
        f"无证据的 fact 有 {outcome.facts_without_evidence} 条",
    )

    return checks


@dataclass
class GoldenReport:
    """整轮 Golden Run 的结果。"""

    outcomes: list[ScenarioOutcome] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    ran_at: str = ""

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks) and bool(self.checks)

    @property
    def total_cost(self) -> float:
        return sum(o.cost for o in self.outcomes)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "passed": self.passed,
            "total_cost": round(self.total_cost, 6),
            "outcomes": [o.as_dict() for o in self.outcomes],
            "checks": [c.as_dict() for c in self.checks],
            "failures": [c.as_dict() for c in self.failures()],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)

    def render_table(self) -> str:
        lines = [
            f"{'场景':<16}{'解析':>6}{'坏行':>6}{'等级':>6}  命中",
            "-" * 66,
        ]
        for outcome in self.outcomes:
            lines.append(
                f"{outcome.scenario:<16}{outcome.parsed:>6}{outcome.bad_lines:>6}"
                f"{outcome.tier:>6}  {', '.join(outcome.anomaly_types) or '—'}"
            )
        lines.append("-" * 66)
        passed = sum(1 for c in self.checks if c.passed)
        lines.append(
            f"断言 {passed}/{len(self.checks)} 通过；合计成本 ¥{self.total_cost:.6f}"
        )
        return "\n".join(lines)


def run_all(
    *,
    session: Session,
    project_ids: dict[str, int],
    uploads_dir: Path,
    router: Any = None,
) -> GoldenReport:
    """跑全部 5 个场景并逐条判定。

    `project_ids` 按场景给一个独立项目：不同场景的数据混在一个 Project 里会
    互相污染（上一场景的事件会留在库里被下一场景一起分析）。
    """
    domain = build_default_registry().load("computer_monitoring")
    report = GoldenReport(ran_at=datetime.now(timezone.utc).isoformat())

    for scenario in SCENARIOS:
        expectation = load_expectation(scenario)
        project_id = project_ids[scenario]
        outcome, result = run_scenario(
            scenario,
            session=session,
            project_id=project_id,
            uploads_dir=uploads_dir,
            router=router,
            domain=domain,
        )
        report.outcomes.append(outcome)
        report.checks.extend(check_expectation(expectation, outcome, result))

    return report
