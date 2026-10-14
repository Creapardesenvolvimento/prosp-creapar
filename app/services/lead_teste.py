"""Lead de teste: para conversar com a Bruna usando o próprio celular antes de prospectar de verdade."""

import logging
import random
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.integrations import evolution
from app.models import Blacklist, Campanha, EnvioStatus, Lead, LeadStatus, Mensagem, NumeroStatus, Porte, WhatsappNumber
from app.services import disparo
from app.services.telefone import normalizar_telefone, para_evolution

logger = logging.getLogger(__name__)


class LeadTesteError(Exception):
    pass


def criar(
    db: Session,
    *,
    nome: str,
    telefone: str,
    numero: WhatsappNumber,
    campanha: Campanha,
    categoria: str = "",
    cidade: str = "",
    segmento: str = "",
    porte: str = "",
) -> Lead:
    """Cria (ou reinicia) o lead e manda a abertura na hora, sem esperar a fila do disparo."""
    e164 = normalizar_telefone(telefone)
    if e164 is None:
        raise LeadTesteError("Telefone inválido. Use DDD + número, ex.: (11) 99999-8888.")
    if numero.status != NumeroStatus.CONECTADO:
        raise LeadTesteError("O número escolhido não está conectado.")
    if numero.telefone == e164:
        raise LeadTesteError("Use um celular diferente do chip que envia; o WhatsApp não manda mensagem para ele mesmo.")
    if not any(m.ativa for m in campanha.mensagens):
        raise LeadTesteError("A campanha escolhida não tem mensagem de abertura.")
    if db.scalar(select(Blacklist.id).where(Blacklist.telefone == e164)):
        raise LeadTesteError("Esse telefone está na blacklist. Tire de lá antes de testar.")
    if porte and porte not in set(Porte):
        raise LeadTesteError("Porte inválido.")
    # O identificador do WhatsApp pode não ser igual ao telefone (celulares antigos aparecem sem o 9).
    jid = evolution.get_client(numero.server).check_whatsapp(numero.instance_name, [para_evolution(e164)]).get(para_evolution(e164))
    if not jid:
        raise LeadTesteError("Esse telefone não tem WhatsApp.")

    lead = db.scalar(select(Lead).where(Lead.telefone == e164))
    if lead is None:
        lead = Lead(telefone=e164)
        db.add(lead)
    else:
        # Reinicia o teste: conversa nova, do zero.
        db.execute(delete(Mensagem).where(Mensagem.lead_id == lead.id))
    lead.nome = nome.strip() or "Empresa de teste"
    lead.categoria = categoria.strip() or None
    lead.cidade = cidade.strip() or None
    lead.whatsapp_jid = jid
    lead.campanha_id = campanha.id
    lead.numero_id = numero.id
    lead.status = LeadStatus.NA_FILA
    lead.segmento = segmento.strip() or None
    lead.porte = porte or None
    lead.etapa = None
    lead.ia_ativa = True
    lead.handoff_motivo = None
    lead.responder_em = None
    lead.respondeu_em = None
    lead.enviado_em = None
    lead.tentativas_envio = 0
    db.flush()

    envio = disparo.enviar_abertura(db, numero, lead, datetime.now(timezone.utc), random.Random())
    if envio is None or envio.status != EnvioStatus.ENVIADO:
        raise LeadTesteError(f"A abertura não foi enviada: {envio.erro if envio else 'telefone bloqueado'}")
    logger.info("lead de teste criado", extra={"lead_id": lead.id, "numero_id": numero.id, "campanha_id": campanha.id})
    return lead
