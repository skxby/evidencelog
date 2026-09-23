"""阶段 01 的冒烟任务：验证 worker 能消费任务并把结果写回 backend。"""

from app.celery_app import celery_app


@celery_app.task(name="health.ping")
def ping(x: int = 1) -> int:
    return x + 1
