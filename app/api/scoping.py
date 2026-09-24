"""按资源 id 解析并校验归属（用于只有 `{run_id}` / `{insight_id}` 的路由）。

**为什么不能在这些路由上用 `ProjectScopeDep`**：它要求路径里有 `{project_id}`，
而 `/api/runs/{run_id}` 没有这个参数 —— 硬套会让 FastAPI 把 `project_id` 当成
缺失的必填项直接返回 422。

正确做法是：**用资源自己的 id 查出来，再校验它所属的 Project 属于当前用户**。
跨项目访问同样返回 404（不暴露该资源是否存在）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

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


def assert_same_project(resource_project_id: int, claimed_project_id: int, *, what: str) -> None:
    """校验"资源自身的 project"与"调用方声明的 project"一致。

    为什么需要它：页面上的 JS 会按模板拼上 `?project_id=…`（页面知道自己在哪个项目里），
    而按资源 id 鉴权的端点并**不读**这个参数 —— 于是它成了一个"看起来在校验、
    其实被忽略"的参数。把两者对一下，跨项目访问就多了一道防线，
    也免得后人以为这个参数有用（本轮已经在别的参数上吃过这个亏）。

    不一致时返回 404 而不是 403：403 会暗示"该 id 存在但不属于你"。
    """
    if int(resource_project_id) != int(claimed_project_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} 不存在"
        )


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
