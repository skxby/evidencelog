"""按资源 id 解析并校验归属（用于只有 `{run_id}` / `{insight_id}` 的路由）。

**为什么不能在这些路由上用 `ProjectScopeDep`**：它要求路径里有 `{project_id}`，
而 `/api/runs/{run_id}` 没有这个参数 —— 硬套会让 FastAPI 把 `project_id` 当成
缺失的必填项直接返回 422。

正确做法是：**用资源自己的 id 查出来，再校验它所属的 Project 属于当前用户**。
跨项目访问同样返回 404（不暴露该资源是否存在）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.models.agent_run import AgentRun
from app.models.insight import Insight
from app.repositories.project import ProjectRepository


def _not_found(what: str, resource_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} {resource_id} 不存在"
    )


def _owns_project(session: Session, user_id: int, project_id: int) -> bool:
    return ProjectRepository(session).get_for_user(user_id, int(project_id)) is not None


@dataclass(frozen=True)
class RunScope:
    """已确认归属当前用户的 Run。"""

    run: AgentRun
    project_id: int

    @property
    def run_id(self) -> int:
        return int(self.run.id)


def get_run_scope(
    session: SessionDep,
    user: CurrentUser,
    run_id: Annotated[int, Path(ge=1)],
) -> RunScope:
    """按 run_id 取 Run 并校验其 Project 归属当前用户。"""
    run = session.get(AgentRun, run_id)
    if run is None or not _owns_project(session, user.id, run.project_id):
        raise _not_found("Run", run_id)
    return RunScope(run=run, project_id=int(run.project_id))


RunScopeDep = Annotated[RunScope, Depends(get_run_scope)]


@dataclass(frozen=True)
class InsightScope:
    """已确认归属当前用户的 Insight。"""

    insight: Insight
    project_id: int

    @property
    def insight_id(self) -> int:
        return int(self.insight.id)


def get_insight_scope(
    session: SessionDep,
    user: CurrentUser,
    insight_id: Annotated[int, Path(ge=1)],
) -> InsightScope:
    """按 insight_id 取 Insight 并校验其 Project 归属当前用户。"""
    insight = session.get(Insight, insight_id)
    if insight is None or not _owns_project(session, user.id, insight.project_id):
        raise _not_found("Insight", insight_id)
    return InsightScope(insight=insight, project_id=int(insight.project_id))


InsightScopeDep = Annotated[InsightScope, Depends(get_insight_scope)]


def assert_same_project(resource_project_id: int, path_project_id: int, *, what: str) -> None:
    """校验嵌套路由里的两者属于同一个 Project。

    例如 `/api/projects/{project_id}/...` 里引用的资源必须属于同一个 project，
    否则就是跨项目访问。返回 404 而不是 403。
    """
    if int(resource_project_id) != int(path_project_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} 不存在"
        )


def owned_project_ids(session: Session, user_id: int) -> set[int]:
    """当前用户拥有的全部 project_id，供批量过滤使用。"""
    return {int(p.id) for p in ProjectRepository(session).list_for_user(user_id)}


def ensure_owned(resource: Any, owned: set[int]) -> Any:
    """对象为空或不属于已拥有的 project 时返回 None。"""
    if resource is None:
        return None
    return resource if int(resource.project_id) in owned else None


def get_owned_project_id(
    session: SessionDep,
    user: CurrentUser,
    project_id: Annotated[int, Query(ge=1, description="该资源所属的 Project id")],
) -> int:
    """从 **query 参数**取 `project_id` 并校验归属。

    给"路径里只有资源 id、没有 project_id"的端点用（如
    `/api/knowledge/candidates/{candidate_id}/confirm`）。这些端点不能用
    `ProjectScopeDep` —— 它要求路径里有 `{project_id}`。

    归属不匹配时返回 404（与其它跨项目访问一致，不暴露资源是否存在）。
    """
    if not _owns_project(session, user.id, project_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"项目 {project_id} 不存在"
        )
    return int(project_id)
