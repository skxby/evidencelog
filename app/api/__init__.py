"""API 层：鉴权、参数校验、任务创建与状态返回。

计划第 856 行：API **不承担分析逻辑** —— 分析一律交给 Celery（阶段 08）。
"""

from __future__ import annotations

from app.api.deps import (
    CurrentUser,
    ProjectScope,
    ProjectScopeDep,
    SessionDep,
    get_current_user,
)
from app.api.security import (
    AuthError,
    InvalidCredentialsError,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

__all__ = [
    "AuthError",
    "CurrentUser",
    "InvalidCredentialsError",
    "ProjectScope",
    "ProjectScopeDep",
    "SessionDep",
    "TokenError",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "hash_password",
    "verify_password",
]
