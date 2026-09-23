"""Celery 实例。

约定（与阶段 08 的可靠性机制对应）：
- broker / backend 都用 Redis，V1 不额外引入依赖；
- 内部时间一律 UTC；
- task_acks_late + reject_on_worker_lost：worker 被杀时任务可被重新投递，
  这是「僵尸 Run 回收」能成立的前提之一。
"""

from celery import Celery

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
