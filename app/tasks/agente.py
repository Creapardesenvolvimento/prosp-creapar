import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.celery_app import celery_app
from app.db import SessionLocal
from app.integrations import chatwoot
from app.integrations.chatwoot import ChatwootError
from app.integrations.evolution import EvolutionError
from app.models import Lead, WhatsappNumber
from app.services import agente, mensagens
from app.services.agenda import utc
from app.tasks.agenda import avisar_vendedor
from app.tenancy import usar_cliente

logger = logging.getLogger(__name__)


def _reivindicar(db, lead_id: int, agora: datetime) -> bool:
    """Só uma tarefa responde: a que encontrar responder_em vencido (mensagens novas o empurram para frente)."""
    result = db.execute(
        update(Lead)
        .where(Lead.id == lead_id, Lead.responder_em.is_not(None), Lead.responder_em <= agora)
        .values(responder_em=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


@celery_app.task(name="agente.responder", bind=True, max_retries=3)
def responder_lead(self, lead_id: int, agora: str | None = None) -> str:
    momento = utc(datetime.fromisoformat(agora)) if agora else datetime.now(timezone.utc)
    with SessionLocal() as db:
        if not _reivindicar(db, lead_id, momento):
            return "aguardando"
        lead = db.get(Lead, lead_id)
        usar_cliente(db, lead.cliente_id)
        try:
            resultado = agente.conversar(db, lead)
            reunioes = db.info.pop("reunioes_novas", [])
            db.commit()
        except EvolutionError as exc:
            db.rollback()
            lead = db.get(Lead, lead_id)
            lead.responder_em = datetime.now(timezone.utc) + timedelta(seconds=60)
            db.commit()
            logger.error("falha ao enviar resposta da IA; nova tentativa em 1 min", extra={"lead_id": lead_id, "erro": str(exc)})
            raise self.retry(countdown=61, exc=exc)
    for reuniao_id in reunioes:
        avisar_vendedor.delay(reuniao_id)
    if resultado == "humano":
        rotular_conversa.delay(lead_id, "humano")
    return resultado


@celery_app.task(name="agente.responder_pendentes")
def responder_pendentes() -> int:
    """Rede de segurança: responde leads cuja tarefa se perdeu (ex.: worker reiniciado)."""
    limite = datetime.now(timezone.utc) - timedelta(seconds=30)
    with SessionLocal() as db:
        ids = list(db.scalars(select(Lead.id).where(Lead.responder_em.is_not(None), Lead.responder_em <= limite)))
    for lead_id in ids:
        responder_lead.delay(lead_id)
    return len(ids)


@celery_app.task(name="agente.conferir_mensagem_propria")
def conferir_mensagem_propria(dados: dict) -> bool:
    with SessionLocal() as db:
        usar_cliente(db, dados.get("cliente_id"))
        lead = mensagens.confirmar_humano(db, dados)
        db.commit()
        lead_id = lead.id if lead else None
    if lead_id:
        rotular_conversa.delay(lead_id, "humano")
    return bool(lead_id)


@celery_app.task(name="agente.rotular_conversa", autoretry_for=(ChatwootError,), retry_backoff=30, max_retries=5)
def rotular_conversa(lead_id: int, label: str) -> bool:
    """Põe a label na conversa do lead no Chatwoot (a caixa de entrada é a do número que fala com ele)."""
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        numero = db.get(WhatsappNumber, lead.numero_id) if lead and lead.numero_id else None
        if numero is None or numero.chatwoot_inbox_id is None:
            return False
        telefone, inbox_id = lead.telefone, numero.chatwoot_inbox_id
    cw = chatwoot.get_client()
    cw.ensure_label(label)
    conversas = cw.conversas_do_telefone(telefone, inbox_id)
    if not conversas:
        logger.warning("conversa do lead não encontrada no Chatwoot", extra={"lead_id": lead_id})
        return False
    cw.add_label(conversas[0], label)
    logger.info("label aplicada no Chatwoot", extra={"lead_id": lead_id, "label": label})
    return True
