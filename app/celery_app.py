"""Celery 实例。

约定（与阶段 08 的可靠性机制对应）：
- broker / backend 都用 Redis，V1 不额外引入依赖；
- 内部时间一律 UTC；
- task_acks_late + reject_on_worker_lost：worker 被杀时任务可被重新投递，
  这是「僵尸 Run 回收」能成立的前提之一。
- **Worker 也要配 structlog**：见文件末尾的说明。
"""

from celery import Celery
from celery.signals import setup_logging

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "logagent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.health", "app.tasks.analysis"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 一次只预取一个任务：分析任务重且耗时长，预取会让队列分配严重不均
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_track_started=True,
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    """在 worker 进程里也启用 structlog。

    真机现象：`configure_logging()` 只在 `app/main.py`（Web 进程）里被调用过，
    worker 进程从来没配过。于是计划第 937 行要求的「JSON 结构化日志」在
    worker 侧不成立，日志里也没有 trace_id —— 第 928 行那句
    「HTTP 请求 → 入队 → Worker 执行 能被同一个 trace_id 串起来」实际断成两截：
    拿着 Run 详情里的 trace_id 去 grep，只能捞到 Web 那半边。

    这个 bug 的隐蔽之处在于：**worker 看起来"有日志"**（Celery 自己的
    INFO/MainProcess 行一直在打），所以没人会怀疑日志没配对。

    用 `setup_logging` 信号而不是模块级直接调用：Celery 在启动时会自己
    `hijack_root_logger`，模块级调用配的东西会被它覆盖掉；接在信号上才是
    "配在 Celery 配完之后"，顺序不会错。
    """
    from app.utils.observability import configure_logging

    configure_logging(level=settings.log_level, force=True)
