import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.integrations.chatwoot import ChatwootError
from app.models import WhatsappNumber
from app.services import numeros as svc
from app.tenancy import clientes_ativos, usar_cliente

logger = logging.getLogger(__name__)


@celery_app.task(name="numeros.conferir_conexoes")
def conferir_conexoes() -> int:
    with SessionLocal() as db:
        return svc.conferir_conexoes(db)


@celery_app.task(name="numeros.virada_do_dia")
def virada_do_dia() -> None:
    with SessionLocal() as db:
        clientes = clientes_ativos(db)
    for cliente_id in clientes:
        with SessionLocal() as db:
            usar_cliente(db, cliente_id)
            svc.garantir_virada(db)
            db.commit()


@celery_app.task(
    name="numeros.marcar_conversas_numero_perdido",
    autoretry_for=(ChatwootError,),
    retry_backoff=30,
    max_retries=5,
)
def marcar_conversas_numero_perdido(numero_id: int) -> int:
    with SessionLocal() as db:
        numero = db.get(WhatsappNumber, numero_id)
        return svc.marcar_conversas_numero_perdido(numero) if numero else 0
