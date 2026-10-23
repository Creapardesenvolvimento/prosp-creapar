import logging
from contextlib import contextmanager

import redis

from app.celery_app import celery_app
from app.config import get_settings
from app.db import SessionLocal
from app.services import disparo
from app.tenancy import clientes_ativos, usar_cliente

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
            clientes = clientes_ativos(db)
        enviados = 0
        for cliente_id in clientes:  # cada cliente com os próprios números, horário, meta e limites
            with SessionLocal() as db:
                usar_cliente(db, cliente_id)
                try:
                    enviados += disparo.ciclo(db)
                except Exception:
                    logger.exception("falha no disparo do cliente", extra={"cliente_id": cliente_id})
        return enviados
