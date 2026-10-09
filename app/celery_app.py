from celery import Celery
from celery.signals import setup_logging as celery_setup_logging

from app.config import get_settings
from app.log import setup_logging

settings = get_settings()

celery_app = Celery("creapar", broker=settings.redis_url, backend=settings.redis_url, include=["app.tasks"])
celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    task_always_eager=settings.celery_always_eager,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    beat_schedule={},
)


@celery_setup_logging.connect
def _configure_logging(**kwargs):
    setup_logging()
