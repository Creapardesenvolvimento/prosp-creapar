from app.celery_app import celery_app
from app.db import SessionLocal
from app.integrations.evolution import EvolutionError
from app.models import Reuniao
from app.services import agenda as svc


@celery_app.task(name="agenda.avisar_vendedor", autoretry_for=(EvolutionError,), retry_backoff=30, max_retries=5)
def avisar_vendedor(reuniao_id: int) -> str | None:
    with SessionLocal() as db:
        reuniao = db.get(Reuniao, reuniao_id)
        if reuniao is None:
            return None
        mensagem_id = svc.avisar_vendedor(db, reuniao)
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
        return mensagem_id
