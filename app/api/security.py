"""JWT 鉴权与口令哈希（计划第 858 行）。

- 方式：**JWT**，`Authorization: Bearer <token>`；网页端存 `httpOnly` Cookie。
- 口令：**bcrypt**，不用 passlib（AGENTS.md 禁止项；passlib 自 2020 起无人维护，
  且与 bcrypt>=4.1 组合会抛 "error reading bcrypt version"）。
- 自用单账号，不做角色系统。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

#: 默认有效期（小时）。自用场景给较长有效期，避免频繁登录。
DEFAULT_TOKEN_TTL_HOURS = 24 * 7

#: 算法固定 HS256（对称密钥，单机自用足够；换算法要同时改签发与校验）
ALGORITHM = "HS256"

#: bcrypt 的工作因子。12 在现代硬件上约 0.2–0.3s，是常用平衡点。
#: 注意 bcrypt 只取口令前 72 字节，超长口令必须显式拒绝而不是静默截断——
#: 静默截断会让两个不同长口令互相可登录。
BCRYPT_ROUNDS = 12
MAX_PASSWORD_BYTES = 72


class AuthError(Exception):
    """鉴权失败。"""


class InvalidCredentialsError(AuthError):
    """账号或口令不对。"""


class TokenError(AuthError):
    """令牌无效或过期。"""


def _password_bytes(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"口令超过 bcrypt 的 {MAX_PASSWORD_BYTES} 字节上限（{len(encoded)} 字节）；"
            "bcrypt 会静默截断导致不同口令互相可登录，故这里显式拒绝"
        )
    return encoded


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希。"""
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。

    任何异常都返回 False 而不是抛出——登录路径不该把内部错误细节暴露出去，
    但**也不静默当作成功**。
    """
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode())
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    subject: str | int,
    secret_key: str,
    ttl_hours: int = DEFAULT_TOKEN_TTL_HOURS,
    extra_claims: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """签发 JWT。"""
    if not secret_key:
        raise ValueError("secret_key 不能为空：JWT 没有密钥等于没有鉴权")
    issued_at = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(hours=ttl_hours)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM)


def decode_access_token(
    token: str, *, secret_key: str, now: datetime | None = None
) -> dict[str, Any]:
    """校验并解出 JWT 载荷。失败一律抛 `TokenError`。"""
    if not token:
        raise TokenError("缺少令牌")
    if not secret_key:
        raise ValueError("secret_key 不能为空")
    try:
        return jwt.decode(
            token,
            secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("令牌已过期") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"令牌无效：{exc}") from exc


def bearer_token_from_header(header_value: str | None) -> str:
    """从 `Authorization: Bearer <token>` 里取出令牌。"""
    if not header_value:
        raise TokenError("缺少 Authorization 头")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise TokenError("Authorization 头格式应为 `Bearer <token>`")
    token = parts[1].strip()
    if not token:
        raise TokenError("Bearer 后没有令牌")
    return token
