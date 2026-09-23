"""analyzer 与 YAML 加载器单测。

覆盖三类容易出问题的地方：
1. analyzer 的**边界条件** —— 样本不足必须"不判定"，不能假装通过（红线 4）；
2. runbook YAML 的**结构校验** —— 坏文件要报错，不能静默跳过；
3. 知识条的**约束** —— 无 evidence / 无 confidence 不允许 confirmed（计划第 494 行）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domains.computer_monitoring.analyzers import (
    CPU_SPIKE_THRESHOLD,
    DEFAULT_ANALYZERS,
    CPUSpike,
    DiskFull,
    MemoryGrowth,
    ProcessCrash,
    run_analyzers,
)
from app.domains.computer_monitoring.knowledge import (
    KnowledgeFormatError,
    assert_confirmable,
    load_all,
    load_staging,
    validate_entry,
)
from app.domains.computer_monitoring.runbooks import (
    RunbookFormatError,
    load_runbooks,
    parse_runbook,
)
from app.domains.protocol import KnowledgeNotConfirmableError

TS = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def metric(name: str, value: float) -> dict:
    return {"metric_name": name, "value": value, "timestamp": TS}


# ============================================================
# CPUSpike
# ============================================================


def test_cpu_spike_fires_on_consecutive_high_samples():
    findings = CPUSpike().analyze([metric("cpu_used", v) for v in (91, 93, 96)])
    assert len(findings) == 1
    assert findings[0].analyzer == "CPUSpike"
    assert findings[0].metric_name == "cpu_used"
    assert findings[0].severity == "high"  # 峰值 > 95


def test_cpu_spike_does_not_fire_on_brief_spike():
    """单点尖峰不是"持续"，不应命中。"""
    assert CPUSpike().analyze([metric("cpu_used", v) for v in (10, 99, 10)]) == []


def test_cpu_spike_requires_enough_samples():
    """样本不足 → 不判定（返回空），**不是**"通过"。"""
    assert CPUSpike().analyze([metric("cpu_used", 99), metric("cpu_used", 99)]) == []


def test_cpu_spike_ignores_other_metrics():
    assert CPUSpike().analyze([metric("memory_used", v) for v in (91, 93, 96)]) == []


def test_cpu_spike_threshold_is_the_documented_one():
    assert CPU_SPIKE_THRESHOLD == 90.0


# ============================================================
# MemoryGrowth
# ============================================================


def test_memory_growth_fires_on_monotonic_rise():
    findings = MemoryGrowth().analyze([metric("memory_used", v) for v in (50, 60, 70, 80)])
    assert len(findings) == 1
    assert findings[0].analyzer == "MemoryGrowth"
    assert findings[0].severity == "medium"


def test_memory_growth_high_when_over_threshold():
    findings = MemoryGrowth().analyze([metric("memory_used", v) for v in (80, 88, 94, 99)])
    assert findings[0].severity == "high"


def test_memory_growth_ignores_fluctuation():
    """抖动后回落不是持续增长 —— 只看首尾会误判。"""
    assert MemoryGrowth().analyze([metric("memory_used", v) for v in (80, 60, 85, 70)]) == []


def test_memory_growth_requires_enough_samples():
    assert MemoryGrowth().analyze([metric("memory_used", 10), metric("memory_used", 90)]) == []


def test_memory_growth_ignores_tiny_rise():
    """涨了一点点不算增长。"""
    assert MemoryGrowth().analyze([metric("memory_used", v) for v in (50, 50.1, 50.2, 50.3)]) == []


# ============================================================
# DiskFull
# ============================================================


def test_disk_full_fires_over_threshold():
    findings = DiskFull().analyze([metric("disk_used", 96.5)])
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].detail["peak"] == 96.5


def test_disk_full_does_not_fire_at_or_below_threshold():
    assert DiskFull().analyze([metric("disk_used", 95.0)]) == []
    assert DiskFull().analyze([metric("disk_used", 10.0)]) == []


def test_disk_full_returns_nothing_without_samples():
    assert DiskFull().analyze([]) == []


# ============================================================
# ProcessCrash
# ============================================================


@pytest.mark.parametrize(
    "message",
    [
        "nginx[9]: segfault at 0 ip 00007f",
        "kernel: Out of memory: Kill process 1234 (java)",
        "systemd: core dumped for unit foo.service",
        "kernel: Kernel panic - not syncing",
    ],
)
def test_process_crash_matches_documented_patterns(message: str):
    findings = ProcessCrash().analyze([{"message": message, "event_id": 7}])
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].event_ids == [7]


def test_process_crash_ignores_normal_messages():
    assert ProcessCrash().analyze([{"message": "service started normally"}]) == []


def test_process_crash_reports_one_finding_per_matching_event():
    findings = ProcessCrash().analyze(
        [
            {"message": "segfault at 0", "event_id": 1},
            {"message": "all good", "event_id": 2},
            {"message": "out of memory", "event_id": 3},
        ]
    )
    assert [f.event_ids for f in findings] == [[1], [3]]


# ============================================================
# run_analyzers 汇总
# ============================================================


def test_run_analyzers_aggregates_all_four():
    samples = [
        metric("cpu_used", v) for v in (91, 93, 96)
    ] + [
        metric("memory_used", v) for v in (50, 60, 70, 80)
    ] + [
        metric("disk_used", 97.0),
        {"message": "segfault at 0", "event_id": 1},
    ]
    names = {f.analyzer for f in run_analyzers(samples)}
    assert names == {"CPUSpike", "MemoryGrowth", "DiskFull", "ProcessCrash"}


def test_default_analyzers_are_the_four_core_ones():
    """计划第 407 行：V1 先落 4 个核心 analyzer。"""
    assert [a.name for a in DEFAULT_ANALYZERS] == [
        "CPUSpike",
        "MemoryGrowth",
        "DiskFull",
        "ProcessCrash",
    ]


def test_analyzers_are_deterministic():
    """同样的输入必须给同样的输出 —— 确定性是红线 1 的前提。"""
    samples = [metric("cpu_used", v) for v in (91, 93, 96)]
    first = run_analyzers(samples)
    for _ in range(3):
        assert run_analyzers(samples) == first


# ============================================================
# runbook YAML 加载
# ============================================================


def test_parse_runbook_requires_mandatory_keys():
    with pytest.raises(RunbookFormatError, match="缺少必需字段"):
        parse_runbook({"id": "x", "title": "t"}, source="t.yaml")


def test_parse_runbook_rejects_empty_steps():
    with pytest.raises(RunbookFormatError, match="steps"):
        parse_runbook(
            {"id": "x", "title": "t", "applies": {}, "steps": []}, source="t.yaml"
        )


def test_parse_runbook_rejects_non_mapping():
    with pytest.raises(RunbookFormatError, match="映射"):
        parse_runbook(["not", "a", "mapping"], source="t.yaml")  # type: ignore[arg-type]


def test_load_runbooks_missing_dir_returns_empty(work_tmp):
    """尚未写 runbook 是合法状态。"""
    assert load_runbooks(work_tmp / "nope") == []


def test_load_runbooks_reports_duplicate_ids(work_tmp):
    """重复 id 必须报错，不能后者静默覆盖前者。"""
    (work_tmp / "a.yaml").write_text(
        "id: dup\ntitle: A\napplies: {}\nsteps: [x]\n", encoding="utf-8"
    )
    (work_tmp / "b.yaml").write_text(
        "id: dup\ntitle: B\napplies: {}\nsteps: [y]\n", encoding="utf-8"
    )
    with pytest.raises(RunbookFormatError, match="重复"):
        load_runbooks(work_tmp)


def test_shipped_runbooks_are_valid_and_non_trivial():
    from pathlib import Path

    books = load_runbooks(
        Path(__file__).resolve().parents[2] / "app/domains/computer_monitoring/runbooks"
    )
    assert len(books) == 5
    for book in books:
        assert len(book.steps) >= 3, f"{book.id} 的步骤太少，不构成可执行 procedure"


# ============================================================
# 知识加载与约束
# ============================================================


def _entry(**overrides) -> dict:
    base = {
        "id": "kp_test_001",
        "kind": "error_pattern",
        "title": "测试模式",
        "match": {"event_type": "metric", "metric_name": "cpu_used", "condition": "value > 90"},
        "evidence": {"run_id": "r1", "event_ids": [1, 2]},
        "confidence": 0.8,
        "status": "confirmed",
    }
    base.update(overrides)
    return base


def test_valid_confirmed_entry_loads():
    entry = validate_entry(_entry())
    assert entry.id == "kp_test_001"
    assert entry.status == "confirmed"
    assert entry.confidence == 0.8


def test_confirmed_without_evidence_is_refused():
    """计划第 494 行：无 evidence 的条目不允许确认。"""
    with pytest.raises(KnowledgeNotConfirmableError, match="缺少 evidence"):
        validate_entry(_entry(evidence={}))


def test_confirmed_without_confidence_is_refused():
    with pytest.raises(KnowledgeNotConfirmableError, match="confidence"):
        validate_entry(_entry(confidence=0.0))


def test_draft_without_evidence_is_allowed():
    """draft 只是候选，可以没有 evidence —— 但它不参与自动结论。"""
    entry = validate_entry(_entry(status="draft", evidence={}, confidence=0.0))
    assert entry.status == "draft"


def test_invalid_kind_is_refused():
    with pytest.raises(KnowledgeFormatError, match="kind"):
        validate_entry(_entry(kind="not_a_kind"))


def test_invalid_status_is_refused():
    with pytest.raises(KnowledgeFormatError, match="status"):
        validate_entry(_entry(status="maybe"))


@pytest.mark.parametrize("condition", ["value > 90", "value < 10", "value = 5", "value >= 1"])
def test_supported_condition_operators(condition: str):
    assert validate_entry(_entry(match={"condition": condition})).match["condition"] == condition


@pytest.mark.parametrize(
    "condition",
    [
        "value ** 2 > 90",           # 通用表达式：V1 明确不做
        "cpu > 90",                  # 左值必须是 value
        "value > abc",               # 比较值必须是数字
        "value > 90 and x < 5",      # 复合条件
    ],
)
def test_unsupported_conditions_are_refused(condition: str):
    with pytest.raises(KnowledgeFormatError):
        validate_entry(_entry(match={"condition": condition}))


def test_staging_candidates_are_forced_to_draft(work_tmp):
    """计划第 493 行：候选不参与自动结论；即便文件写了 confirmed 也要降级。"""
    path = work_tmp / "candidates.yaml"
    path.write_text(
        "- id: cand_1\n"
        "  kind: error_pattern\n"
        "  title: 候选\n"
        "  evidence: {run_id: r1, event_ids: [1]}\n"
        "  confidence: 0.9\n"
        "  status: confirmed\n",
        encoding="utf-8",
    )
    entries = load_staging(path)
    assert len(entries) == 1
    assert entries[0].status == "draft"


def test_runtime_confirmed_overrides_seed(work_tmp):
    """修订说明第 2 条：先读 seed，再叠加运行时 confirmed（运行时覆盖 seed）。"""
    from pathlib import Path

    domain_dir = Path(__file__).resolve().parents[2] / "app/domains/computer_monitoring"
    data_dir = work_tmp / "data"
    runtime = data_dir / "knowledge" / "computer_monitoring" / "confirmed"
    runtime.mkdir(parents=True)

    # seed 为空时，运行时条目应当出现在结果里
    runtime.joinpath("x.yaml").write_text(
        "- id: rt_1\n"
        "  kind: error_pattern\n"
        "  title: 运行时\n"
        "  evidence: {run_id: r1, event_ids: [1]}\n"
        "  confidence: 0.7\n"
        "  status: confirmed\n",
        encoding="utf-8",
    )
    result = load_all(domain_dir, data_dir=data_dir, domain_id="computer_monitoring")
    assert [e.id for e in result["confirmed"]] == ["rt_1"]


def test_shipped_seed_knowledge_is_empty_by_design():
    """计划第 498 行：知识内容必须来自真实经验，不代填。"""
    from pathlib import Path

    domain_dir = Path(__file__).resolve().parents[2] / "app/domains/computer_monitoring"
    entries = load_all(domain_dir)["confirmed"]
    assert entries == []


def test_assert_confirmable_accepts_run_id_without_event_ids():
    entry = validate_entry(_entry(evidence={"run_id": "r1", "event_ids": []}))
    assert_confirmable(entry)  # 不抛异常
