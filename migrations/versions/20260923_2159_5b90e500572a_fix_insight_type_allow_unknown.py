"""fix_insight_type_allow_unknown

修复：把 `unknown` 补进 insights.type 的 CHECK 约束。

计划第 811 行给模型的输出 schema 是四值
（`fact | inference | possibility | unknown`），第 829–836 行明确要求
「四层语义在三层同时强制」：schema、prompt、报告页渲染。
prompt（pipeline 第 3 条）与报告页（「− 未知」+ 信息缺口）都已实现第四层，
只有数据库约束当初漏了 `unknown`。

后果是真机上才暴露的：模型按 schema 给出 type="unknown" 的**合法**结论，
落库时被 `ck_insights_insight_type` 拒掉，整次分析以任务崩溃收场。
故这不是放宽约束，而是把实现补齐到计划要求的四层。

Revision ID: 5b90e500572a
Revises: f893dcf4c86d
Create Date: 2026-09-23 21:59:11.626737

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "5b90e500572a"
down_revision: str | None = "f893dcf4c86d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 约束的**短名**。库里最终叫 `ck_insights_insight_type`，
#: 由 base.py 的命名约定 `ck_%(table_name)s_%(constraint_name)s` 拼出；
#: 这里若直接写全名，约定会再拼一次前缀，变成
#: `ck_insights_ck_insights_insight_type` 而找不到约束（实测踩过）。
CONSTRAINT_NAME = "insight_type"

#: 迁移脚本里的取值**写成字面量**，不 import app.models.enums：
#: 迁移是历史快照，必须永远可重放。若引用会变的枚举，
#: 将来枚举再改一次，这条迁移的含义就跟着变了。
UPGRADED_VALUES = ("fact", "inference", "possibility", "unknown")
ORIGINAL_VALUES = ("fact", "inference", "possibility")


def _check_sql(values: tuple[str, ...]) -> str:
    return "type IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "insights", type_="check")
    op.create_check_constraint(CONSTRAINT_NAME, "insights", _check_sql(UPGRADED_VALUES))


def downgrade() -> None:
    # 回滚前必须先把已存在的 `unknown` 行改成 `possibility`。
    # 不处理的话，重建三值约束会因存量数据直接失败 —— 那等于交了一个
    # 跑不通的 downgrade（db-migration 技能判定为"未完成"）。
    #
    # 这一步**有损**：`unknown`（信息不足，提不出候选）与
    # `possibility`（有候选但无法确认）在语义上不同，回滚会把两者合并。
    # 属于有意接受的损失，在此写明。
    op.execute(
        "UPDATE insights SET type = 'possibility' WHERE type = 'unknown'"
    )
    op.drop_constraint(CONSTRAINT_NAME, "insights", type_="check")
    op.create_check_constraint(CONSTRAINT_NAME, "insights", _check_sql(ORIGINAL_VALUES))
