"""FastAPI 依赖：鉴权与 Project 作用域（阶段 10 验收第 1、4 条）。

两条纪律：
1. **每个业务端点都必须经 `CurrentUser` / `ProjectScope`**，不允许直接裸查；
2. 跨 Project 访问一律 404（而不是 403）——403 会泄露"这个 id 存在但不属于你"。
   自用单账号场景下 404 更干净：不暴露任何他人的资源是否存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.api.security import TokenError, bearer_token_from_header, decode_access_token
from app.config import get_settings
from app.db import get_db
from app.models.project import Project
from app.models.user import User
from app.repositories.project import ProjectRepository
from app.repositories.user import UserRepository

SessionDep = Annotated[Session, Depends(get_db)]


def _unauthorized(detail: str, *, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=headers or {"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """从 `Authorization: Bearer <token>` 解出当前用户。"""
    settings = get_settings()
    try:
        token = bearer_token_from_header(authorization)
        payload = decode_access_token(token, secret_key=settings.secret_key)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    subject = payload.get("sub")
    if subject is None:
        raise _unauthorized("令牌缺少 sub")

    try:
        user_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise _unauthorized(f"令牌 sub 不是合法用户 id: {subject!r}") from exc

    user = UserRepository(session).get(user_id)
    if user is None:
        # 令牌有效但用户已被删除：当作未授权，不泄露"该用户曾存在"
        raise _unauthorized("令牌对应的用户不存在")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@dataclass(frozen=True)
class ProjectScope:
    """一个已确认归属当前用户的 Project。

    所有带 `project_id` 的端点都应依赖它，而不是自己去查——
    这样"跨 Project 访问被拒绝"就是机制保证，不是每个端点各自记得检查。
    """

    project: Project
    user: User

    @property
    def project_id(self) -> int:
        return int(self.project.id)


def get_project_scope(
    session: SessionDep,
    user: CurrentUser,
    project_id: Annotated[int, Path(ge=1)],
) -> ProjectScope:
    """解析 `{project_id}` 路径参数并校验归属。"""
    project = ProjectRepository(session).get_for_user(user.id, project_id)
    if project is None:
        # 刻意用 404：403 会暗示"这个项目存在但不属于你"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"项目 {project_id} 不存在",
        )
    return ProjectScope(project=project, user=user)


ProjectScopeDep = Annotated[ProjectScope, Depends(get_project_scope)]


def require_json_body(body: Any) -> Any:
    """占位：保留给后续需要显式校验 body 非空的端点。

    目前 FastAPI 的 Pydantic 模型已覆盖这一点，故这里只做类型标注用途。
    """
    return body
