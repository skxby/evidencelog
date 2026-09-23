"""Repository 基类：**强制** project_id 过滤。

计划第 360–363 行的隔离实现要求：「所有读写走统一的 repository 函数，内部强制带
project_id 条件；不依赖『开发者记得过滤』的裸查询」。

本基类把这条要求变成机制而不是纪律：
- 带 project 边界的模型，查询入口只有 `scoped()` 一个，它**必须**接收 project_id；
- 想绕过就得显式调 `unscoped()`，名字本身即审计线索。

User 表不属于任何 project，故不继承本基类。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class ProjectScopedRepository(Generic[ModelT]):
    """所有业务表的读写入口。

    子类只需声明 `model`；查询一律经 `scoped()`，写入断言 project_id 一致。
    """

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------- 查询 ----------

    def scoped(self, project_id: int) -> Select[tuple[ModelT]]:
        """返回已带 project_id 过滤的 select。

        **这是唯一常规查询入口。** 所有自定义查询都应从它派生：
            repo.scoped(pid).where(Event.severity == "high")
        """
        if project_id is None:
            raise ValueError("project_id 不能为空：Project 是上下文边界")
        return select(self.model).where(self.model.project_id == project_id)  # type: ignore[attr-defined]

    def unscoped(self) -> Select[tuple[ModelT]]:
        """不带 project 过滤的查询。

        仅供**运维/迁移脚本**使用。业务代码调用它会破坏 Project 隔离，
        故名字刻意取得刺眼，便于在 review 与 grep 中暴露。
        """
        return select(self.model)

    def get(self, project_id: int, obj_id: int) -> ModelT | None:
        """按 id 取单条，**同时**校验归属，拿不到别家数据。"""
        return self.session.execute(
            self.scoped(project_id).where(self.model.id == obj_id)  # type: ignore[attr-defined]
        ).scalar_one_or_none()

    def list_all(self, project_id: int) -> list[ModelT]:
        return list(self.session.execute(self.scoped(project_id)).scalars().all())

    def count(self, project_id: int) -> int:
        from sqlalchemy import func

        return int(
            self.session.execute(
                select(func.count()).select_from(self.model).where(
                    self.model.project_id == project_id  # type: ignore[attr-defined]
                )
            ).scalar_one()
        )

    # ---------- 写入 ----------

    def add(self, project_id: int, obj: ModelT) -> ModelT:
        """插入前断言归属一致，防止把 A 项目的数据写进 B 项目。"""
        obj_project_id = getattr(obj, "project_id", None)
        if obj_project_id != project_id:
            raise ValueError(
                f"{type(obj).__name__}.project_id={obj_project_id!r} 与传入的 "
                f"project_id={project_id!r} 不一致：拒绝跨项目写入"
            )
        self.session.add(obj)
        return obj

    def delete(self, project_id: int, obj_id: int) -> bool:
        """按 id 删除，同样先校验归属。"""
        obj = self.get(project_id, obj_id)
        if obj is None:
            return False
        self.session.delete(obj)
        return True
