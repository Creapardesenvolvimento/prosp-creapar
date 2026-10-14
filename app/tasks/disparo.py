import logging
from contextlib import contextmanager

import redis

from app.celery_app import celery_app
from app.config import get_settings
from app.db import SessionLocal
from app.services import disparo

logger = logging.getLogger(__name__)


@contextmanager
def _trava(nome: str, segundos: int):
    """Impede duas rodadas do disparo ao mesmo tempo (o beat pode disparar antes de a anterior acabar)."""
    settings = get_settings()
    if settings.celery_always_eager:
        yield True
        return
    lock = redis.Redis.from_url(settings.redis_url).lock(f"creapar:{nome}", timeout=segundos, blocking=False)
    if not lock.acquire():
        yield False
        return
    try:
        yield True
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            pass  # expirou antes de terminar; a próxima rodada segue normalmente


@celery_app.task(name="disparo.ciclo")
def ciclo_disparo() -> int:
    with _trava("disparo", 300) as livre:
        if not livre:
            return 0
        with SessionLocal() as db:
            return disparo.ciclo(db)
