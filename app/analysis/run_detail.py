"""Run 详情：把「系统做了什么、花了多少、为何得到这个结论」组装成一份可读记录。

计划第 940 行的验收是这句话能否**完整复述**。所以本模块不是简单地把表行
拼起来，而是把散落在各处的证据按"审阅者会问的问题"重新组织：

    这个 Run 是谁触发的、什么时候、属于哪个 Project
      → 跑了哪些阶段、每个阶段什么状态
      → 调了几次模型、用的哪个等级/型号、各花了多少 token 与成本
      → 有没有重试 / 降级 / 取消 / 超时，原因是什么
      → 策略检查点当时是怎么判的
      → 最终得到哪些结论、每条挂在什么证据上

**只读取，不修改**；所有数据经 repository 的 project 边界取得。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.analysis.state_machine import allowed_targets
from app.models.enums import AGENT_RUN_STATES


@dataclass
class RunDetail:
    """一个 Run 的完整可审阅记录。"""

    run_id: int
    project_id: int
    status: str
    trace_id: str | None = None

    # ---- 时间线 ----
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    last_heartbeat: datetime | None = None

    # ---- 阶段进度 ----
    current_phase: str | None = None
    phase_history: list[dict[str, Any]] = field(default_factory=list)
    completed_phases: list[str] = field(default_factory=list)
    skipped_phases: list[str] = field(default_factory=list)

    # ---- 花费 ----
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    model_call_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_actual: float = 0.0
    #: 按等级汇总（L1/L2/L3 各花了多少）
    cost_by_tier: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---- 工具 ----
    tool_usage: dict[str, Any] = field(default_factory=dict)
    tool_call_count: int = 0

    # ---- 异常路径 ----
    error: str | None = None
    stop_reason: str | None = None
    note: str | None = None
    cancel_requested: bool = False
    retries: list[dict[str, Any]] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)

    # ---- 策略 ----
    policy: dict[str, Any] = field(default_factory=dict)

    # ---- 结论与证据 ----
    insights: list[dict[str, Any]] = field(default_factory=list)
    evidence_by_insight: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    # ---- 自检 ----
    #: 复述过程中发现的"说不通"之处（例如 fact 没有证据），必须显式暴露
    anomalies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "status": self.status,
            "trace_id": self.trace_id,
            "timeline": {
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_seconds": self.duration_seconds,
                "last_heartbeat": self.last_heartbeat,
            },
            "phases": {
                "current": self.current_phase,
                "history": self.phase_history,
                "completed": self.completed_phases,
                "skipped": self.skipped_phases,
            },
            "cost": {
                "model_call_count": self.model_call_count,
                "tokens_input": self.tokens_input,
                "tokens_output": self.tokens_output,
                "cost_actual": round(self.cost_actual, 6),
                "by_tier": self.cost_by_tier,
                "calls": self.model_calls,
            },
            "tools": {
                "call_count": self.tool_call_count,
                "usage": self.tool_usage,
            },
            "outcome": {
                "error": self.error,
                "stop_reason": self.stop_reason,
                "note": self.note,
                "cancel_requested": self.cancel_requested,
                "retries": self.retries,
                "fallbacks": self.fallbacks,
            },
            "policy": self.policy,
            "insights": self.insights,
            "evidence_by_insight": {str(k): v for k, v in self.evidence_by_insight.items()},
            "anomalies": self.anomalies,
        }

    def narrate(self) -> str:
        """把这条记录复述成一段人话。

        这是验收的**直接检验**：如果叙述里出现空缺（"未知"、"未记录"），
        说明可观测性还不到位。
        """
        lines: list[str] = []
        lines.append(
            f"Run #{self.run_id}（项目 {self.project_id}，trace {self.trace_id or '未记录'}）"
            f" 状态 {self.status}。"
        )
        if self.started_at:
            lines.append(
                f"开始于 {self.started_at.isoformat()}，"
                f"耗时 {self.duration_seconds:.1f}s。" if self.duration_seconds is not None
                else f"开始于 {self.started_at.isoformat()}，尚未结束。"
            )
        if self.completed_phases:
            lines.append("完成的阶段：" + "、".join(self.completed_phases) + "。")
        if self.skipped_phases:
            lines.append("跳过的阶段：" + "、".join(self.skipped_phases) + "。")

        lines.append(
            f"共调用模型 {self.model_call_count} 次，"
            f"输入 {self.tokens_input} / 输出 {self.tokens_output} tokens，"
            f"花费 ¥{self.cost_actual:.4f}。"
        )
        if self.cost_by_tier:
            details = "，".join(
                f"{tier} {info['calls']} 次 ¥{info['cost']:.4f}"
                for tier, info in sorted(self.cost_by_tier.items())
            )
            lines.append("分级花费：" + details + "。")

        if self.tool_call_count:
            lines.append(f"调用工具 {self.tool_call_count} 次。")

        if self.retries:
            lines.append(f"发生过 {len(self.retries)} 次重试。")
        if self.fallbacks:
            lines.append(f"发生过 {len(self.fallbacks)} 次模型降级。")

        if self.stop_reason:
            lines.append(f"中断原因：{self.stop_reason}。{self.note or ''}")
        if self.error:
            lines.append(f"错误：{self.error}")
        if self.cancel_requested:
            lines.append("该 Run 收到过取消请求。")

        if not self.insights:
            lines.append("没有产出结论。")
        else:
            lines.append(f"产出 {len(self.insights)} 条结论：")
            for insight in self.insights:
                evidence_ids = insight.get("evidence_event_ids") or []
                lines.append(
                    f"  - [{insight['type']}] {insight['title']}"
                    f"（{len(evidence_ids)} 条证据）"
                )

        if self.anomalies:
            lines.append("⚠ 复述时发现的问题：" + "；".join(self.anomalies))
        return "\n".join(lines)


def build_run_detail(
    *,
    run: Any,
    insights: list[Any],
    evidence_by_insight: dict[int, list[Any]],
    expected_policy: dict[str, Any] | None = None,
) -> RunDetail:
    """组装 Run 详情。纯读，不做任何写操作。"""
    metadata = dict(run.run_metadata or {})

    # ---- 花费：以 model_calls 明细为准（它是每次调用的原始记录） ----
    calls = list(run.model_calls or [])
    cost_by_tier: dict[str, dict[str, Any]] = {}
    for call in calls:
        tier = str(call.get("tier") or "unknown")
        bucket = cost_by_tier.setdefault(tier, {"calls": 0, "cost": 0.0, "tokens_input": 0, "tokens_output": 0})
        bucket["calls"] += 1
        bucket["cost"] = round(bucket["cost"] + float(call.get("cost") or 0), 6)
        bucket["tokens_input"] += int(call.get("tokens_input") or 0)
        bucket["tokens_output"] += int(call.get("tokens_output") or 0)

    tool_usage = dict(run.tool_usage or {})
    tool_call_count = sum(
        len(entries) for entries in tool_usage.values() if isinstance(entries, list)
    )

    duration: float | None = None
    if run.started_at and run.finished_at:
        duration = (run.finished_at - run.started_at).total_seconds()

    phase_history = list(run.phase_history or [])

    detail = RunDetail(
        run_id=int(run.id),
        project_id=int(run.project_id),
        status=run.status,
        trace_id=metadata.get("trace_id"),
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=duration,
        last_heartbeat=run.last_heartbeat,
        current_phase=run.current_phase,
        phase_history=phase_history,
        completed_phases=metadata.get("completed_phases")
        or [e["phase"] for e in phase_history if e.get("status") == "done"],
        skipped_phases=metadata.get("skipped_phases")
        or [e["phase"] for e in phase_history if e.get("status") == "skipped"],
        model_calls=calls,
        model_call_count=len(calls),
        tokens_input=int(run.tokens_input or 0),
        tokens_output=int(run.tokens_output or 0),
        cost_actual=float(run.cost_actual or 0),
        cost_by_tier=cost_by_tier,
        tool_usage=tool_usage,
        tool_call_count=tool_call_count,
        error=run.error,
        stop_reason=metadata.get("stop_reason"),
        note=metadata.get("note"),
        cancel_requested=bool(run.cancel_requested),
        retries=list(metadata.get("retries") or []),
        fallbacks=list(metadata.get("fallbacks") or metadata.get("attempts") or []),
        policy=dict(metadata.get("policy") or expected_policy or {}),
    )

    detail.insights = [_insight_row(i, evidence_by_insight) for i in insights]
    detail.evidence_by_insight = {
        int(insight_id): [_evidence_row(e) for e in rows]
        for insight_id, rows in evidence_by_insight.items()
    }

    detail.anomalies = audit_run_detail(detail)
    return detail


def _insight_row(insight: Any, evidence_by_insight: dict[int, list[Any]]) -> dict[str, Any]:
    rows = evidence_by_insight.get(int(insight.id), [])
    event_ids: list[Any] = []
    for row in rows:
        event_ids.extend(row.event_ids or [])
    metadata = dict(insight.run_metadata or {})
    return {
        "id": int(insight.id),
        "type": insight.type,
        "severity": insight.severity,
        "confidence": float(insight.confidence),
        "title": insight.title,
        "summary": insight.summary,
        "reasoning": insight.reasoning,
        "limitations": insight.limitations,
        # 把证据里的事件 id 汇总出来，"为何得到这个结论"要能一眼看到依据
        "evidence_event_ids": event_ids,
        "evidence_count": len(rows),
        "runbooks": metadata.get("runbooks") or [],
    }


def _evidence_row(evidence: Any) -> dict[str, Any]:
    return {
        "id": int(evidence.id),
        "insight_id": int(evidence.insight_id),
        "event_ids": list(evidence.event_ids or []),
        "time_range": evidence.time_range,
        "calculation": evidence.calculation,
        "description": evidence.description,
    }


def audit_run_detail(detail: RunDetail) -> list[str]:
    """复述时的自检：找出"说不通"的地方并显式列出。

    这里查的都是**本该不可能**的情况。它们一旦出现，说明上游某道闸门被绕过；
    静默通过会让"可追踪"变成"看起来可追踪"。
    """
    problems: list[str] = []

    if detail.status not in AGENT_RUN_STATES:
        problems.append(f"状态 {detail.status!r} 不在 7 态之内")

    # 红线 3：fact 必须有证据
    for insight in detail.insights:
        if insight["type"] == "fact" and not insight["evidence_event_ids"]:
            problems.append(f"fact「{insight['title']}」没有任何证据事件")

    # 花费一致性：明细之和应当等于汇总值
    detail_cost = round(sum(float(c.get("cost") or 0) for c in detail.model_calls), 6)
    if detail.model_calls and abs(detail_cost - round(detail.cost_actual, 6)) > 1e-6:
        problems.append(
            f"成本不一致：明细合计 ¥{detail_cost} 与汇总 ¥{detail.cost_actual:.6f} 不符"
        )

    detail_tokens_in = sum(int(c.get("tokens_input") or 0) for c in detail.model_calls)
    if detail.model_calls and detail_tokens_in != detail.tokens_input:
        problems.append(
            f"输入 token 不一致：明细 {detail_tokens_in} 与汇总 {detail.tokens_input} 不符"
        )

    # 终态必须有结束时间
    from app.models.enums import AGENT_RUN_TERMINAL_STATES

    if detail.status in AGENT_RUN_TERMINAL_STATES and detail.finished_at is None:
        problems.append(f"状态 {detail.status} 是终态但没有结束时间")

    # 部分成功 / 失败必须给出原因
    if detail.status in ("partial_success", "failed", "timeout") and not (
        detail.stop_reason or detail.error
    ):
        problems.append(f"状态 {detail.status} 但既没有 stop_reason 也没有 error")

    # 还在 running 却没有心跳 —— 僵尸回收将无从判断
    if detail.status == "running" and detail.last_heartbeat is None:
        problems.append("状态为 running 但从未写过心跳")

    # 阶段历史里出现了不该出现的状态字面量
    for entry in detail.phase_history:
        if not isinstance(entry, dict) or "phase" not in entry:
            problems.append(f"阶段历史条目形状异常：{entry!r}")

    return problems


def allowed_next_statuses(status: str) -> list[str]:
    """当前状态下允许转移到哪些状态（复用状态机，不另写一份）。"""
    try:
        return sorted(allowed_targets(status))
    except Exception:  # noqa: BLE001 - 非法状态不影响详情展示
        return []
