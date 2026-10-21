import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.integrations.apify import ApifyError
from app.integrations.evolution import EvolutionError
from app.models import Busca
from app.services import abastecimento
from app.services import buscas as svc
from app.services.buscas import BuscaError

logger = logging.getLogger(__name__)


@celery_app.task(name="buscas.iniciar")
def iniciar_busca(busca_id: int) -> None:
    with SessionLocal() as db:
        busca = db.get(Busca, busca_id)
        if busca:
            svc.iniciar(db, busca)
            db.commit()


@celery_app.task(name="buscas.processar")
def processar_busca(busca_id: int) -> None:
    with SessionLocal() as db:
        if not svc.reivindicar_processamento(db, busca_id):
            return  # outro gatilho (webhook ou polling) já pegou esta busca
        busca = db.get(Busca, busca_id)
        try:
            # Guarda tudo antes de validar: se a validação falhar, os dados pagos ficam no banco.
            svc.guardar_resultados(db, busca)
            db.commit()
            svc.processar(db, busca)
            db.commit()
        except (BuscaError, ApifyError, EvolutionError) as exc:
            db.rollback()
            svc.marcar_erro(db, db.get(Busca, busca_id), str(exc))
            db.commit()


@celery_app.task(name="buscas.conferir_andamento")
def conferir_buscas() -> None:
    """Fallback do webhook do Apify: consulta os runs que ainda constam como executando."""
    with SessionLocal() as db:
        prontas = []
        for busca in svc.buscas_em_andamento(db):
            try:
                run = svc.apify.get_client().get_run(busca.apify_run_id)
            except ApifyError as exc:
                logger.error("falha ao consultar run do Apify", extra={"busca_id": busca.id, "erro": str(exc)})
                continue
            if svc.registrar_fim_do_run(db, busca, run):
                prontas.append(busca.id)
        db.commit()
    for busca_id in prontas:
        processar_busca.delay(busca_id)


@celery_app.task(name="buscas.abastecer")
def abastecer() -> int | None:
    """Beat: se a fila de leads baixou, dispara a próxima busca do plano."""
    with SessionLocal() as db:
        busca = abastecimento.abastecer(db)
        db.commit()
        busca_id = busca.id if busca else None
    if busca_id:
        iniciar_busca.delay(busca_id)
    return busca_id
