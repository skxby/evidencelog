"""数据库连接与会话。

阶段 02 起，所有业务读写都必须走 repository 函数并强制带 project_id；
这里只提供 engine / session / Base，不做任何业务判断。
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    # 连接被中间层掐断后自动重建，避免长跑的 worker 拿到死连接
    pool_pre_ping=True,
    # 池耗尽时最多等 30s，然后明确报错，而不是无限挂住
    pool_timeout=30,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（阶段 02 起使用）。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的会话工厂。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
