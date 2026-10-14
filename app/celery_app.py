from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging as celery_setup_logging

from app.config import get_settings
from app.log import setup_logging

settings = get_settings()

celery_app = Celery("creapar", broker=settings.redis_url, backend=settings.redis_url, include=["app.tasks", "app.tasks.numeros", "app.tasks.agenda", "app.tasks.buscas"])
celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    task_always_eager=settings.celery_always_eager,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "conferir-conexoes": {
            "task": "numeros.conferir_conexoes",
            "schedule": settings.conferir_conexoes_segundos,
        },
        "conferir-buscas": {
            "task": "buscas.conferir_andamento",
            "schedule": 120,
        },
        "virada-do-dia": {
            "task": "numeros.virada_do_dia",
            "schedule": crontab(hour=0, minute=0),
        },
    },
)


@celery_setup_logging.connect
def _configure_logging(**kwargs):
    setup_logging()
