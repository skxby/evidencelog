"""声明式基类与公共类型。

`Base.metadata` 是 Alembic autogenerate 的比对源（见 migrations/env.py）。
约束命名约定统一在此定义，保证约束名可预测、迁移可回滚。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

# 显式命名约定：否则不同数据库生成的约束名不一致，downgrade 时删不掉
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


#: 所有时间列统一用「带时区的时间戳」，库里存 UTC。
#: 容器 TZ=UTC（阶段 01 已验），缺时区的解析由 DEFAULT_TIMEZONE 负责，那是阶段 04 的事。
TimestampTZ = DateTime(timezone=True)


def utcnow() -> datetime:
    """返回带时区的当前 UTC 时间。

    刻意不用 `datetime.utcnow()`：它返回 naive 时间，与 TIMESTAMPTZ 列配合
    会产生「看起来对、实际丢了时区」的静默错误。
    """
    from datetime import timezone

    return datetime.now(timezone.utc)
