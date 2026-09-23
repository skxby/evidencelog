"""鉴权与项目端点。

API 只做验证、鉴权、创建任务、返回状态，**不承担分析逻辑**（计划第 856 行）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import CurrentUser, ProjectScopeDep, SessionDep
from app.api.schemas import (
    CreateDataSourceRequest,
    CreateProjectRequest,
    DataSourceResponse,
    LoginRequest,
    ProjectResponse,
    RegisterRequest,
    TokenResponse,
)
from app.api.security import create_access_token, hash_password, verify_password
from app.config import get_settings
from app.models.datasource import DataSource
from app.models.project import Project
from app.models.user import User
from app.repositories.datasource import DataSourceRepository
from app.repositories.project import ProjectRepository
from app.repositories.user import UserRepository

router = APIRouter()


# ============================================================
# 鉴权
# ============================================================


@router.post("/api/register", response_model=TokenResponse, tags=["auth"])
def register(payload: RegisterRequest, session: SessionDep) -> TokenResponse:
    """注册（自用场景可省略注册页，但端点保留以便初始化账号）。"""
    users = UserRepository(session)
    if users.get_by_email(payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册"
        )

    user = users.add(User(email=payload.email, password_hash=hash_password(payload.password)))
    session.flush()
    return TokenResponse(access_token=create_access_token(subject=user.id, secret_key=get_settings().secret_key))


@router.post("/api/login", response_model=TokenResponse, tags=["auth"])
def login(
    payload: LoginRequest, session: SessionDep, response: Response
) -> TokenResponse:
    """登录：返回 JWT，同时写入 `httpOnly` Cookie（计划第 858 行）。"""
    user = UserRepository(session).get_by_email(payload.email)
    # 刻意不区分"用户不存在"与"口令不对"，避免枚举账号
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或口令不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(subject=user.id, secret_key=get_settings().secret_key)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,           # 计划要求：token 存 httpOnly Cookie
        samesite="lax",
        secure=False,            # 本机 http 自用；上线需置 True 并配 HTTPS
        max_age=60 * 60 * 24 * 7,
    )
    return TokenResponse(access_token=token)


@router.get("/api/me", tags=["auth"])
def me(user: CurrentUser) -> dict:
    return {"id": int(user.id), "email": user.email}


# ============================================================
# Project
# ============================================================


@router.post(
    "/api/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
def create_project(
    payload: CreateProjectRequest, session: SessionDep, user: CurrentUser
) -> ProjectResponse:
    project = ProjectRepository(session).add(
        Project(
            user_id=user.id,
            name=payload.name,
            budget_total=payload.budget_total,
            budget_used=0,
        )
    )
    session.flush()
    return _project_response(project)


@router.get("/api/projects", response_model=list[ProjectResponse], tags=["projects"])
def list_projects(session: SessionDep, user: CurrentUser) -> list[ProjectResponse]:
    return [
        _project_response(p)
        for p in ProjectRepository(session).list_for_user(user.id)
    ]


# ============================================================
# DataSource
# ============================================================


@router.post(
    "/api/projects/{project_id}/data-sources",
    response_model=DataSourceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["data-sources"],
)
def create_data_source(
    payload: CreateDataSourceRequest, scope: ProjectScopeDep, session: SessionDep
) -> DataSourceResponse:
    source = DataSourceRepository(session).add(
        scope.project_id,
        DataSource(
            project_id=scope.project_id,
            type=payload.type,
            format=payload.format,
            location=payload.location,
            meta=payload.metadata,
        ),
    )
    session.flush()
    return _data_source_response(source)


@router.get(
    "/api/projects/{project_id}/data-sources",
    response_model=list[DataSourceResponse],
    tags=["data-sources"],
)
def list_data_sources(scope: ProjectScopeDep, session: SessionDep) -> list[DataSourceResponse]:
    return [
        _data_source_response(s)
        for s in DataSourceRepository(session).list_all(scope.project_id)
    ]


# ============================================================
# 序列化
# ============================================================


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=int(project.id),
        name=project.name,
        status=project.status,
        budget_total=float(project.budget_total),
        budget_used=float(project.budget_used),
    )


def _data_source_response(source: DataSource) -> DataSourceResponse:
    return DataSourceResponse(
        id=int(source.id),
        project_id=int(source.project_id),
        type=source.type,
        format=source.format,
        location=source.location,
        status=source.status,
    )
