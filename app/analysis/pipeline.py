"""分析主链路（计划第 735–751 行）。

```text
加载事件
  → 指标计算（产出 metric 事件）
  → 模板聚类 / 事件分组（EventGroup）
  → 事故归并（Incident，同时间窗 / 同签名；可复用已有 Incident）
  → analyzers / confirmed 知识命中
  → 异常检测（V1 仅文件内统计）
  → 复杂度评估（决定 L0–L3）
  → Context 构造与蒸馏（含历史相似 Incident，按等级 token 预算）
  → 模型分析（同一次调用同时输出 insights 与 knowledge_candidates）
  → Evidence 校验（event_id 子集校验、幻觉拦截）
  → Insight 落库；候选知识写入 staging
```

**执行顺序是固定的**（计划第 754–760 行），不能打乱：
    1. analyzers 产出候选异常
    2. confirmed error_patterns 增强解释 / 补充命中
    3. false_positives 最后裁决 —— 命中则抑制该（误报）异常

顺序的意义：误报抑制必须在最后，否则它无法抑制前两步新产生的异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.analysis.complexity import (
    ComplexityAssessment,
    assess_complexity,
    count_time_clusters,
)
from app.analysis.context import DistilledContext, build_distilled_context
from app.analysis.evidence import (
    MAX_EVIDENCE_RETRIES,
    ValidatedInsight,
    build_retry_feedback,
    validate_insights,
)
from app.analysis.grouping import (
    DEFAULT_GROUP_WINDOW_SECONDS,
    DEFAULT_MERGE_WINDOW_SECONDS,
    GroupableEvent,
    MergeCandidate,
    cluster_events,
    merge_into_incidents,
)
from app.gateways.base import TIER_L0
from app.tools.data_ops import stats_calculator

#: 计划第 748–749 行：同一次调用同时输出 insights 与 knowledge_candidates
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["fact", "inference", "possibility", "unknown"]},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "confidence": {"type": "number"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "reasoning": {"type": "string"},
                    "limitations": {"type": "string"},
                },
                "required": ["type", "severity", "confidence", "title", "summary"],
            },
        },
        "knowledge_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "match": {"type": "object"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "severity_hint": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
    "required": ["insights"],
}


@dataclass
class PipelineInput:
    """一次分析的输入。"""

    project_id: int
    source_id: int
    domain_id: str
    domain_version: str
    time_start: datetime
    time_end: datetime
    events: list[dict[str, Any]] = field(default_factory=list)
    #: 历史相似事故（计划第 787 行：注入 Context）
    historical_incidents: list[dict[str, Any]] = field(default_factory=list)
    #: confirmed 知识（来自领域目录包，阶段 03 的加载器）
    confirmed_knowledge: list[Any] = field(default_factory=list)


@dataclass
class PipelineResult:
    """一次分析的结果。"""

    tier: str
    complexity: ComplexityAssessment
    context_tokens: int
    insights: list[ValidatedInsight] = field(default_factory=list)
    knowledge_candidates: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    rules_only_report: dict[str, Any] | None = None
    #: 模型调用与重试的留痕
    model_attempts: list[dict[str, Any]] = field(default_factory=list)
    evidence_rejections: int = 0
    notes: list[str] = field(default_factory=list)
    valid_event_ids: list[str] = field(default_factory=list)

    @property
    def used_rules_only(self) -> bool:
        return self.tier == TIER_L0 or self.rules_only_report is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "complexity": self.complexity.as_dict(),
            "context_tokens": self.context_tokens,
            "insight_count": len(self.insights),
            "knowledge_candidate_count": len(self.knowledge_candidates),
            "group_count": len(self.groups),
            "incident_count": len(self.incidents),
            "anomaly_count": len(self.anomalies),
            "evidence_rejections": self.evidence_rejections,
            "used_rules_only": self.used_rules_only,
            "notes": list(self.notes),
        }


# ============================================================
# 各步骤
# ============================================================


def build_valid_event_ids(events: list[dict[str, Any]]) -> set[str]:
    """收集本次有效 event_id（计划第 841 行）——幻觉校验的比对基准。"""
    return {str(e["event_id"]) for e in events if e.get("event_id") is not None}


def compute_statistics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """统计（复用阶段 05 的 stats_calculator，不重复实现）。"""
    return stats_calculator(events)


def run_detection(
    events: list[dict[str, Any]],
    *,
    analyzers: list[Any],
    confirmed_knowledge: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按固定顺序执行检测（计划第 754–760 行）。

    返回 `(候选异常列表, 说明)`。
    """
    notes: list[str] = []
    samples = [
        {
            "event_id": e.get("event_id"),
            "message": e.get("message", ""),
            "metric_name": (e.get("payload") or {}).get("metric_name"),
            "value": (e.get("payload") or {}).get("value"),
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity", "low"),
            # 进程名要传下去：崩溃迹象常常只在进程名里
            # （如 CrashReporterSupportHelper），只看 message 会漏检。
            "proc": (e.get("metadata") or {}).get("proc") or e.get("proc"),
        }
        for e in events
    ]

    # ---- 1. analyzers 产出候选异常 ----
    anomalies: list[dict[str, Any]] = []
    for analyzer in analyzers:
        for finding in analyzer.analyze(samples):
            anomalies.append(
                {
                    "type": finding.analyzer,
                    "severity": finding.severity,
                    "message": finding.message,
                    "metric_name": finding.metric_name,
                    "event_ids": list(finding.event_ids),
                    "detail": dict(finding.detail),
                }
            )
    notes.append(f"analyzers 产出 {len(anomalies)} 条候选异常")

    # ---- 2. confirmed error_patterns 增强解释 / 补充命中 ----
    knowledge = confirmed_knowledge or []
    suppressed: list[int] = []
    for entry in knowledge:
        kind = getattr(entry, "kind", None)
        if kind != "error_pattern":
            continue
        for anomaly in anomalies:
            if _knowledge_matches(entry, anomaly):
                anomaly.setdefault("knowledge", []).append(
                    {"id": getattr(entry, "id", ""), "title": getattr(entry, "title", "")}
                )
    if knowledge:
        notes.append(f"confirmed 知识 {len(knowledge)} 条参与增强")

    # ---- 3. false_positives 最后裁决：命中则抑制 ----
    for entry in knowledge:
        if getattr(entry, "kind", None) != "false_positive":
            continue
        for index, anomaly in enumerate(anomalies):
            if _knowledge_matches(entry, anomaly):
                suppressed.append(index)
    if suppressed:
        for index in sorted(set(suppressed), reverse=True):
            del anomalies[index]
        notes.append(f"false_positive 抑制了 {len(set(suppressed))} 条候选异常")

    return anomalies, notes


def _knowledge_matches(entry: Any, anomaly: dict[str, Any]) -> bool:
    """用知识条目的 match 判是否命中某条候选异常。

    V1 只支持固定操作符（计划第 462、501 行），故这里只做：
    `metric_name` 相等 + `condition` 形如 `value <op> <number>`。
    """
    match = getattr(entry, "match", None) or {}
    metric_name = match.get("metric_name")
    if metric_name and metric_name != anomaly.get("metric_name"):
        return False

    condition = match.get("condition")
    if not condition:
        # 只有 message_pattern 的知识条目按正则匹配
        pattern = getattr(entry, "message_pattern", None)
        if pattern:
            import re

            return bool(re.search(str(pattern), str(anomaly.get("message", "")), re.IGNORECASE))
        return bool(metric_name) and metric_name == anomaly.get("metric_name")

    value = (anomaly.get("detail") or {}).get("peak") or (anomaly.get("detail") or {}).get("last")
    if value is None:
        return False
    parts = str(condition).split()
    if len(parts) != 3 or parts[0] != "value":
        return False
    operator, threshold_text = parts[1], parts[2]
    try:
        threshold = float(threshold_text)
    except ValueError:
        return False

    comparisons = {
        ">": value > threshold,
        "<": value < threshold,
        "=": value == threshold,
        ">=": value >= threshold,
        "<=": value <= threshold,
        "!=": value != threshold,
    }
    return bool(comparisons.get(operator, False))


# ============================================================
# 主链路
# ============================================================


def analyze(
    pipeline_input: PipelineInput,
    *,
    analyzers: list[Any],
    domain: Any = None,
    router: Any = None,
    context_budgets: Any = None,
    group_window_seconds: int = DEFAULT_GROUP_WINDOW_SECONDS,
    merge_window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
    max_output_tokens: int = 2000,
    enable_self_critique: bool = True,
    record: bool = False,
    project_id_for_recording: int | None = None,
    run_id: int | None = None,
) -> PipelineResult:
    """跑完整条链路。

    `router=None` 时**只做确定性部分**（L0 纯规则报告）——这让链路在没有模型
    的情况下也可测、可跑，也让"模型不可用降级到 L0"这条路径有真实实现。
    """
    events = list(pipeline_input.events)
    valid_ids = build_valid_event_ids(events)

    # ---- 指标计算 + 统计 ----
    statistics = compute_statistics(events)

    # ---- 模板聚类 / 分组 ----
    groupable = [
        GroupableEvent(
            event_id=int(e["event_id"]),
            source_id=int(e.get("source_id") or pipeline_input.source_id),
            timestamp=e.get("timestamp"),
            message=str(e.get("message", "")),
            severity=str(e.get("severity", "low")),
            event_type=str(e.get("event_type", "log")),
        )
        for e in events
        if e.get("event_id") is not None
    ]
    formed = cluster_events(groupable, window_seconds=group_window_seconds)
    groups = [
        {
            "group_key": g.group_key,
            "source_id": g.source_id,
            "template": g.template,
            "event_ids": list(g.event_ids),
            "event_count": g.event_count,
            "time_start": g.time_start,
            "time_end": g.time_end,
        }
        for g in formed
    ]

    # ---- 检测（固定顺序）----
    anomalies, detection_notes = run_detection(
        events, analyzers=analyzers, confirmed_knowledge=pipeline_input.confirmed_knowledge
    )

    # 命中异常附对应 runbook（计划第 89、909 行：报告页只读展示处置步骤）。
    # 放在这里而不是页面层：runbook 属领域资产，页面不该知道去哪找它。
    attached = attach_runbooks(anomalies, domain=domain)
    if attached:
        detection_notes.append(f"{attached} 条异常附上了对应的 runbook")

    # ---- 事故归并（在检测之后：签名来自 analyzer）----
    signature_by_group: dict[int, str] = {}
    for index, group in enumerate(groups):
        group["_index"] = index
        matched = next(
            (
                a
                for a in anomalies
                if set(a.get("event_ids") or []) & set(group["event_ids"])
            ),
            None,
        )
        signature_by_group[index] = str(matched["type"]) if matched else "unclassified"

    candidates = [
        MergeCandidate(
            group_id=index,
            source_id=group["source_id"],
            severity=_group_severity(group, events),
            time_start=group["time_start"],
            time_end=group["time_end"],
            signature=signature_by_group[index],
        )
        for index, group in enumerate(groups)
    ]
    incidents = merge_into_incidents(
        candidates,
        merge_window_seconds=merge_window_seconds,
        existing_incidents=pipeline_input.historical_incidents,
    )
    for group in groups:
        group.pop("_index", None)

    # ---- 复杂度评估 ----
    complexity = assess_complexity(
        total_events=len(events),
        error_count=int(statistics.get("error_count", 0)),
        high_severity_count=sum(1 for a in anomalies if a.get("severity") == "high"),
        anomaly_count=len(anomalies),
        time_cluster_count=count_time_clusters([e.get("timestamp") for e in events]),
        event_type_count=len(
            [k for k, v in (statistics.get("event_type_counts") or {}).items() if v]
        ),
        anomaly_types=len({str(a.get("type")) for a in anomalies}),
    )

    result = PipelineResult(
        tier=complexity.tier,
        complexity=complexity,
        context_tokens=0,
        groups=groups,
        incidents=incidents,
        anomalies=anomalies,
        statistics=statistics,
        notes=list(detection_notes),
        valid_event_ids=sorted(valid_ids, key=lambda x: (len(x), x)),
    )

    # ---- L0：纯代码统计报告，不调用模型 ----
    if complexity.tier == TIER_L0 or router is None:
        result.rules_only_report = _build_rules_only_report(
            statistics=statistics, groups=groups, incidents=incidents
        )
        if router is None and complexity.tier != TIER_L0:
            result.notes.append("未提供 router，仅产出确定性结果（等价于模型不可用的降级路径）")
        else:
            result.notes.append("L0：无 error 且无 anomaly，产出纯代码统计报告，未调用模型")
        return result

    # ---- Context 组装与蒸馏 ----
    samples = _select_samples(events)
    context = build_distilled_context(
        tier=complexity.tier,
        metadata={
            "domain": pipeline_input.domain_id,
            "domain_version": pipeline_input.domain_version,
            "time_start": str(pipeline_input.time_start),
            "time_end": str(pipeline_input.time_end),
            "event_count": len(events),
        },
        statistics=statistics,
        incidents=_incidents_for_context(incidents, pipeline_input.historical_incidents),
        anomalies=[
            {
                "type": a.get("type"),
                "severity": a.get("severity"),
                "event_ids": a.get("event_ids"),
                "message": a.get("message"),
            }
            for a in anomalies
        ],
        samples=samples,
        context_notes=detection_notes,
    )
    result.context_tokens = context.total_tokens
    if context.compression_notes:
        result.notes.extend(context.compression_notes)

    # ---- 模型分析（含幻觉校验与带反馈重试）----
    _run_model_analysis(
        result,
        context=context,
        router=router,
        complexity_tier=complexity.tier,
        max_output_tokens=max_output_tokens,
        enable_self_critique=enable_self_critique,
        record=record,
        project_id=project_id_for_recording or pipeline_input.project_id,
        run_id=run_id,
    )
    return result


def _group_severity(group: dict[str, Any], events: list[dict[str, Any]]) -> str:
    ranks = {"low": 0, "medium": 1, "high": 2}
    worst = "low"
    ids = set(group.get("event_ids") or [])
    for event in events:
        if event.get("event_id") in ids:
            severity = str(event.get("severity", "low"))
            if ranks.get(severity, 0) > ranks.get(worst, 0):
                worst = severity
    return worst


def _select_samples(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """错误事件全给，警告/正常事件采样（计划第 789 行）。"""
    errors = [e for e in events if str(e.get("severity", "")).lower() == "high"]
    others = [e for e in events if str(e.get("severity", "")).lower() != "high"]
    # 采样式取前 N 条（确定性：不用 random，保证同输入同输出）
    return errors + others[:200]


def _incidents_for_context(
    incidents: list[dict[str, Any]], historical: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """本次归并的事故 + 历史相似事故（计划第 787 行）。"""
    out = [
        {
            "title": f"{incident.get('signature')} 相关事故",
            "severity": incident.get("severity"),
            "group_ids": incident.get("group_ids"),
            "source_id": incident.get("source_id"),
        }
        for incident in incidents
    ]
    for item in historical:
        out.append(
            {
                "title": f"[历史] {item.get('title', '')}",
                "severity": item.get("severity"),
                "root_cause": item.get("root_cause"),
                "historical": True,
            }
        )
    return out


def _build_rules_only_report(
    *,
    statistics: dict[str, Any],
    groups: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
) -> dict[str, Any]:
    """L0 / 模型不可用时产出的纯规则报告。

    措辞必须让读者知道**模型没有参与**（计划第 725 行：页面标注），
    否则它看起来和正常分析一样。
    """
    return {
        "kind": "rules_only",
        "disclaimer": "本报告由确定性规则产出，未经模型分析，结论范围有限",
        "statistics": statistics,
        "group_count": len(groups),
        "incident_count": len(incidents),
        "top_groups": sorted(groups, key=lambda g: -g.get("event_count", 0))[:10],
    }


def _run_model_analysis(
    result: PipelineResult,
    *,
    context: DistilledContext,
    router: Any,
    complexity_tier: str,
    max_output_tokens: int,
    enable_self_critique: bool,
    record: bool,
    project_id: int,
    run_id: int | None,
) -> None:
    """模型分析 + 幻觉校验 + 带反馈重试（计划第 748–748、840–848 行）。"""
    valid_ids = set(result.valid_event_ids)
    prompt = _build_prompt(context)

    round_limit = 2 if (complexity_tier == "L3" and enable_self_critique) else 1
    accepted: list[ValidatedInsight] = []

    for round_index in range(round_limit):
        try:
            model_result = router.generate(
                tier=complexity_tier,
                prompt=prompt,
                schema=ANALYSIS_SCHEMA,
                max_output_tokens=max_output_tokens,
                project_id=project_id if record else None,
                run_id=run_id if record else None,
            )
        except Exception as exc:  # noqa: BLE001 - 模型失败要走降级，不能把链路打断
            result.notes.append(
                f"模型调用失败（{type(exc).__name__}: {exc}），已降级为纯规则报告"
            )
            result.rules_only_report = _build_rules_only_report(
                statistics=result.statistics,
                groups=result.groups,
                incidents=result.incidents,
            )
            return

        payload = model_result.parsed or {}
        result.model_attempts.append(
            {
                "round": round_index + 1,
                "tokens_input": model_result.tokens_input,
                "tokens_output": model_result.tokens_output,
                "cost": model_result.cost,
            }
        )

        outcome = validate_insights(payload.get("insights"), valid_ids=valid_ids)
        accepted.extend(outcome.accepted)
        result.evidence_rejections += len(outcome.rejected)

        # 候选知识同样要过 evidence 子集校验（计划第 827 行）
        result.knowledge_candidates.extend(
            _validate_candidates(payload.get("knowledge_candidates"), valid_ids)
        )

        if not outcome.needs_retry:
            break

        if round_index + 1 >= MAX_EVIDENCE_RETRIES or round_index + 1 >= round_limit:
            # 重试仍失败 → fact 已在 validate_insight 里降级为 possibility
            result.notes.append(
                f"第 {round_index + 1} 轮仍有 {len(outcome.rejected)} 条因证据无效被拒绝，"
                "已达重试上限，未再重试"
            )
            break

        feedback = build_retry_feedback(outcome)
        result.notes.append(f"第 {round_index + 1} 轮证据校验未过，带反馈重试")
        prompt = f"{_build_prompt(context)}\n\n{feedback}"

    # 把 runbook 快照挂到结论上，供报告页只读展示处置步骤（计划第 909 行）
    attach_runbooks_to_insights(accepted, result.anomalies)
    result.insights = accepted


def _build_prompt(context: DistilledContext) -> str:
    return (
        "你是日志分析助手。下面是某段时间窗口内的日志蒸馏数据，请据此给出结论。\n"
        "硬性要求：\n"
        "1. 只有能引用具体 event_id 的结论才能标 fact；evidence_ids 必须是下面出现过的 id。\n"
        "2. 基于证据的推断标 inference，并写出 reasoning。\n"
        "3. 无法确认的标 possibility 或 unknown，并在 limitations 说明缺什么。\n"
        "4. 不要编造 event_id —— 引用不存在的 id 会导致整条结论被拒绝。\n"
        "5. 同时输出针对「现有知识未覆盖」的异常的 knowledge_candidates（可为空数组）。\n\n"
        f"{context.render()}"
    )


def _validate_candidates(raw: Any, valid_ids: set[str]) -> list[dict[str, Any]]:
    """校验候选知识：同样要求 evidence 是本次有效 id 的子集（计划第 827 行）。"""
    if not raw or not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        from app.analysis.evidence import normalize_evidence_ids

        evidence = normalize_evidence_ids(item.get("evidence_ids"))
        kept = [e for e in evidence if e in valid_ids]
        if evidence and not kept:
            # 证据全无效的候选不进 staging —— 不允许凭空总结（计划第 482 行）
            continue
        out.append(
            {
                "kind": str(item.get("kind", "error_pattern")),
                "title": str(item.get("title", "")),
                "description": str(item.get("description", "")),
                "match": item.get("match") or {},
                "evidence_ids": kept,
                "severity_hint": item.get("severity_hint"),
                "confidence": float(item.get("confidence") or 0.0),
                # 候选一律 draft，人工确认前不参与自动结论（计划第 493 行）
                "status": "draft",
            }
        )
    return out

# ============================================================
# runbook 挂接（计划第 89、412、909 行）
# ============================================================


def attach_runbooks(anomalies: list[dict[str, Any]], *, domain: Any) -> int:
    """给命中 runbook 的异常挂上处置步骤（只读展示）。

    返回挂上的条数。`domain` 为空或没有 runbook 时不做任何事——
    runbook 是可选资产，缺了不该让分析失败。
    """
    if domain is None or not hasattr(domain, "find_runbook_for"):
        return 0

    attached = 0
    for anomaly in anomalies:
        book = domain.find_runbook_for(
            str(anomaly.get("type", "")),
            str(anomaly.get("message", "")),
            anomaly.get("metric_name"),
        )
        if book is None:
            continue
        anomaly["runbook"] = {
            "id": book.id,
            "title": book.title,
            "steps": list(book.steps),
            "references": list(book.references),
        }
        attached += 1
    return attached


def runbook_by_analyzer(anomalies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 analyzer 名索引已挂上的 runbook，供结论匹配使用。"""
    return {
        str(a.get("type")): a["runbook"]
        for a in anomalies
        if isinstance(a.get("runbook"), dict)
    }


def attach_runbooks_to_insights(
    insights: list[Any], anomalies: list[dict[str, Any]]
) -> None:
    """把 runbook 与命中的 analyzer 一并写进 Insight 的 extras。

    为什么挂在 extras 而不是单独建表：runbook 是**领域代码资产**（随目录包进
    Git），不是运行数据；把它的内容复制进库会让"改了 YAML 但历史报告还是旧步骤"。
    这里只存一份**当时的快照**用于展示，来源仍以领域目录为准。
    """
    books = runbook_by_analyzer(anomalies)
    if not books:
        return
    for insight in insights:
        extras = getattr(insight, "extras", None)
        if extras is None:
            continue
        # 结论与异常没有强绑定关系（模型可能归纳多条异常），
        # 故把所有命中的 runbook 都附上，由页面展示为"相关处置步骤"。
        extras["runbooks"] = list(books.values())
