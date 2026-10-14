import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.integrations.evolution import EvolutionError
from app.models import Reuniao
from app.services import agenda as svc

logger = logging.getLogger(__name__)


@celery_app.task(name="agenda.avisar_vendedor", autoretry_for=(EvolutionError,), retry_backoff=30, max_retries=5)
def avisar_vendedor(reuniao_id: int) -> str | None:
    """Reunião nova: avisa o vendedor e o WhatsApp de notificações.

    Commit depois de cada aviso: se o segundo falhar, a nova tentativa não repete o primeiro.
    """
    with SessionLocal() as db:
        reuniao = db.get(Reuniao, reuniao_id)
        if reuniao is None:
            return None
        mensagem_id = svc.avisar_vendedor(db, reuniao)
        db.commit()
        svc.notificar_agendamento(db, reuniao)
        db.commit()
        return mensagem_id


@celery_app.task(name="agenda.avisar_cancelamento", autoretry_for=(EvolutionError,), retry_backoff=30, max_retries=5)
def avisar_cancelamento(reuniao_id: int) -> str | None:
    with SessionLocal() as db:
        reuniao = db.get(Reuniao, reuniao_id)
        if reuniao is None:
            return None
        mensagem_id = svc.avisar_cancelamento(db, reuniao)
        db.commit()
        svc.notificar_cancelamento(db, reuniao)
        db.commit()
        return mensagem_id


@celery_app.task(name="agenda.resumo_automatico")
def resumo_automatico() -> int | None:
    """Beat a cada 5 min. Se o envio falhar, nada fica marcado e o próximo ciclo tenta de novo."""
    with SessionLocal() as db:
        try:
            quantidade = svc.resumo_automatico(db)
        except (svc.AgendaError, EvolutionError) as exc:
            db.rollback()
            logger.warning("resumo do dia não enviado", extra={"erro": str(exc)})
            return None
        db.commit()
        return quantidade
