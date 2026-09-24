"""analyzer 与 YAML 加载器单测。

覆盖三类容易出问题的地方：
1. analyzer 的**边界条件** —— 样本不足必须"不判定"，不能假装通过（红线 4）；
2. runbook YAML 的**结构校验** —— 坏文件要报错，不能静默跳过；
3. 知识条的**约束** —— 无 evidence / 无 confidence 不允许 confirmed（计划第 494 行）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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

# ============================================================
# 回归：真实数据暴露出来的两个漏判
# ============================================================


def test_cpu_spike_is_detected_when_the_run_is_followed_by_a_drop():
    """尖峰后回落仍须命中。

    最早的实现用"跑到最后还剩多少连续计数"，峰值后面只要跟着一个正常采样点，
    计数就归零 —— 真实日志几乎总是这样（流量回落后才写日志）。
    原来的单测输入恰好以高值结尾，所以一直没暴露。
    """
    values = [70, 72, 86, 91, 93, 94, 95, 94, 92, 88]
    findings = CPUSpike().analyze([metric("cpu_used", v) for v in values])
    assert len(findings) == 1, "峰值后回落被误判为「没有持续」"
    assert findings[0].detail["consecutive_samples"] == 6
    assert findings[0].detail["peak"] == 95.0


def test_cpu_spike_trailing_drop_does_not_mask_a_brief_spike():
    """回归不能把判定放宽：单点尖峰仍不算持续。"""
    assert CPUSpike().analyze([metric("cpu_used", v) for v in [10, 99, 10]]) == []


def test_process_crash_matches_the_process_name_too():
    """崩溃迹象常常只在进程名里。

    真实样本：`CrashReporterSupportHelper[252]: Internal name did not resolve...`
    —— 消息本身完全正常，但是崩溃上报程序在说话。只看 message 会漏掉整类行。
    """
    findings = ProcessCrash().analyze(
        [{"message": "Internal name did not resolve to internal address!",
          "proc": "CrashReporterSupportHelper", "event_id": 7}]
    )
    assert len(findings) == 1
    assert findings[0].detail["matched_in"] == "进程名"
    assert findings[0].event_ids == [7]


def test_process_crash_still_matches_the_message():
    findings = ProcessCrash().analyze(
        [{"message": "kernel: segfault at 0", "proc": "kernel", "event_id": 1}]
    )
    assert len(findings) == 1
    assert findings[0].detail["matched_in"] == "消息"


def test_process_crash_ignores_ordinary_traffic():
    assert ProcessCrash().analyze(
        [{"message": "service started normally", "proc": "systemd", "event_id": 1}]
    ) == []


# ============================================================
# 回归（2026-09-24 真机核验）：runbook 必须认 analyzer 实际命中的那个词
# ============================================================


def test_runbook_matches_the_keyword_the_analyzer_actually_hit():
    """真机核验：runbook 一条都挂不上 —— 命中可能来自**进程名**。

    `CrashReporterSupportHelper` 这类崩溃上报进程，命中的是 `crash`，
    而 `rb_crash_001` 当时只认 segfault / segmentation fault / core dumped / panic。
    做法：关键词表对齐，并且把 analyzer 实际命中的词一起参与匹配。
    """
    from app.analysis.pipeline import attach_runbooks
    from app.domains.wiring import build_default_registry

    domain = build_default_registry().load("computer_monitoring")
    real_message = (
        "进程异常迹象（进程名命中 “crash”）：Internal name did not resolve to internal address!"
    )

    # 只给消息（真机上的原始形态）→ 现在的关键词表也能命中
    assert domain.find_runbook_for("ProcessCrash", real_message).id == "rb_crash_001"

    anomalies = [
        {
            "type": "ProcessCrash",
            "severity": "high",
            "message": real_message,
            "event_ids": [1],
            "detail": {"pattern": "crash", "matched_in": "进程名"},
        }
    ]
    assert attach_runbooks(anomalies, domain=domain) == 1
    assert anomalies[0]["runbook"]["id"] == "rb_crash_001"
    assert anomalies[0]["runbook"]["steps"], "runbook 要带上处置步骤，页面才有东西可展示"


def test_runbook_does_not_match_a_different_analyzer():
    """关键词对齐不能放宽成"谁来都挂"：analyzer 名必须一致。"""
    from app.domains.wiring import build_default_registry

    domain = build_default_registry().load("computer_monitoring")
    assert domain.find_runbook_for("DiskFull", "进程异常迹象（命中 “crash”）") is None


# ============================================================
# 回归（2026-09-24 真机核验）：坏的 staging 不能带走已确认知识
# ============================================================


def test_broken_staging_does_not_disable_confirmed_knowledge(work_tmp):
    """staging 是模型写的：它坏掉只该让候选失效。

    真机核验：模型自造 `kind=new_pattern` → `load_staging` 抛
    `KnowledgeFormatError` → 整个 `knowledge()` 失败 → `confirmed_knowledge=0`，
    知识闭环断在最后一步（确认过的知识明明在 confirmed/ 里却读不到）。
    """
    from app.domains.computer_monitoring.knowledge import load_all

    base = work_tmp / "knowledge" / "computer_monitoring"
    (base / "confirmed").mkdir(parents=True, exist_ok=True)
    (base / "staging").mkdir(parents=True, exist_ok=True)
    (base / "confirmed" / "ok.yaml").write_text(
        "- id: k_ok\n"
        "  kind: error_pattern\n"
        "  title: 人工确认过的模式\n"
        "  evidence: {run_id: '1'}\n"
        "  confidence: 0.9\n"
        "  status: confirmed\n",
        encoding="utf-8",
    )
    (base / "staging" / "candidates.yaml").write_text(
        "- id: cand_bad\n  kind: new_pattern\n  title: 模型自造的词\n", encoding="utf-8"
    )

    domain_dir = Path(__file__).resolve().parents[2] / "app" / "domains" / "computer_monitoring"
    loaded = load_all(domain_dir, data_dir=work_tmp, domain_id="computer_monitoring")

    assert [e.id for e in loaded["confirmed"] if e.id == "k_ok"] == ["k_ok"], (
        "staging 坏了，已确认的知识也跟着没了"
    )
    assert loaded["staging"] == [], "坏掉的 staging 应当被跳过（并留下告警日志）"


def test_broken_runtime_confirmed_file_does_not_disable_the_others(work_tmp):
    """同一条原则用在 confirmed 上：**代码资产严格、运行时数据容错**。

    seed（仓库内）坏了要当场炸 —— 那是代码 bug；运行时 confirmed 在数据卷里，
    可能是旧版本或手工编辑写坏的，跳过它并指名告警，其余知识照常生效。
    """
    from app.domains.computer_monitoring.knowledge import load_all

    base = work_tmp / "knowledge" / "computer_monitoring"
    (base / "confirmed").mkdir(parents=True, exist_ok=True)
    (base / "confirmed" / "good.yaml").write_text(
        "- id: k_good\n"
        "  kind: error_pattern\n"
        "  title: 好条目\n"
        "  evidence: {run_id: '1'}\n"
        "  confidence: 0.9\n"
        "  status: confirmed\n",
        encoding="utf-8",
    )
    (base / "confirmed" / "bad.yaml").write_text(
        "- id: k_bad\n  kind: new_pattern\n  title: 坏条目\n", encoding="utf-8"
    )

    domain_dir = Path(__file__).resolve().parents[2] / "app" / "domains" / "computer_monitoring"
    loaded = load_all(domain_dir, data_dir=work_tmp, domain_id="computer_monitoring")
    assert [e.id for e in loaded["confirmed"] if e.id == "k_good"] == ["k_good"]
    assert loaded["staging"] == []


def test_bad_entry_does_not_take_down_its_neighbours_in_the_same_file(work_tmp):
    """同一个文件里：坏条目被跳过，好条目照常生效（逐条容错）。

    真机核验（2026-09-24）：确认后的知识写进 `runtime_confirmed.yaml`，
    同文件里还有一条模型自造的 `kind=new_pattern` —— 整份文件抛错，
    人工确认的那条也一起读不回来。
    """
    from app.domains.computer_monitoring.knowledge import load_all

    base = work_tmp / "knowledge" / "computer_monitoring"
    (base / "confirmed").mkdir(parents=True, exist_ok=True)
    (base / "confirmed" / "runtime_confirmed.yaml").write_text(
        "- id: k_bad\n"
        "  kind: new_pattern\n"
        "  title: 模型自造的词\n"
        "- id: k_confirmed_by_human\n"
        "  kind: error_pattern\n"
        "  title: 人工确认过的\n"
        "  evidence: {run_id: '1'}\n"
        "  confidence: 0.8\n"
        "  status: confirmed\n",
        encoding="utf-8",
    )

    domain_dir = Path(__file__).resolve().parents[2] / "app" / "domains" / "computer_monitoring"
    loaded = load_all(domain_dir, data_dir=work_tmp, domain_id="computer_monitoring")
    ids = [e.id for e in loaded["confirmed"]]
    assert "k_confirmed_by_human" in ids, "同文件里的坏条目把好条目也带走了"
    assert "k_bad" not in ids
