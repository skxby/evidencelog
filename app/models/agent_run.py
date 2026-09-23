"""AgentRun —— 一次分析运行。

`status`（7 态生命周期）与 `current_phase` / `phase_history`（跑到哪一步）**正交**，
不要混进一个字段（计划第 329、354 行）。

`model_calls` / `tool_usage` 存 JSON 数组——计划第 351 行明确「不单独建表」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums
from app.models.base import Base, TimestampTZ


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            enums.agent_run_check_sql("agent_runs.status"),
            name="agent_run_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # failed 不可原地复活，只能新建 Run —— 用 parent_run_id 串起谱系（计划第 692 行）
    parent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # 幂等键：相同键且已有成功 Run 则复用结果，不重复执行/计费（计划第 699–703 行）
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=enums.AGENT_RUN_QUEUED, index=True
    )
    # 阶段进度：与 status 正交
    current_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phase_history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    # 僵尸回收依据：心跳丢失超阈值 → 置 timeout（计划第 705–707 行）
    last_heartbeat: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    # 取消标记（计划第 709 行）：Worker 在阶段边界与每次调用前检查它。
    # 刻意用**独立布尔列**而不是塞进 run_metadata：
    #   - 它是执行期被反复轮询的「控制标志」，由 API 进程写、Worker 进程读；
    #   - run_metadata 按计划第 354 行是「最终结果容器」（stop_reason /
    #     completed_phases / skipped_phases），语义不同；
    #   - 每次检查都读整个 JSONB 并解析，既慢又容易出现"改了没生效"。
    # 诚实说明粒度：若恰好进入一次长模型调用，最坏要等该调用返回，
    # 不承诺「秒停」（计划第 710 行）。
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # 模型调用记录（JSON 数组），不单独建表
    model_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    tool_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 货币单位：人民币元
    cost_actual: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 只放 stop_reason / completed_phases / skipped_phases，不再当阶段进度容器
    run_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<AgentRun id={self.id} status={self.status!r} phase={self.current_phase!r}>"
