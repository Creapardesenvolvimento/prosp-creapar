"""Mensagens recebidas pelos números (webhook MESSAGES_UPSERT da Evolution).

Nesta fase: marca que o lead respondeu e trata o pedido de opt-out.
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Autor, Blacklist, Direcao, Envio, Lead, LeadStatus, Mensagem

logger = logging.getLogger(__name__)

# Palavras que, sozinhas, são um pedido para parar ("para" fica de fora: "para quem?").
_PALAVRAS_SAIR = {
    "sair", "saia", "pare", "parar", "stop", "cancelar", "cancela", "descadastrar", "descadastra",
    "remover", "remova", "excluir", "chega", "bloquear",
}
# Estas valem mesmo com mais uma ou duas palavras ("quero sair", "sair por favor").
_PALAVRAS_SAIR_FORTES = {"sair", "stop", "descadastrar", "descadastra", "descadastre"}
# Frases que pedem claramente para não ser contatado, em qualquer ponto da mensagem.
_FRASES_SAIR = [
    r"nao (quero|desejo) (mais )?receber",
    r"(pare|para|parem|parar) de (me )?(mandar|enviar|chamar|mandarem)",
    r"nao (me )?(mande|mandem|envie|enviem|chame|chamem|contate|contatem|procure|procurem)",
    r"(me )?(tira|tire|tirem|remove|remova|removam|exclui|exclua) (meu numero|o meu numero|da (sua |essa |esta )?lista|desta lista|dessa lista)",
    r"nao (entre|entrem) (mais )?em contato",
    r"(nao tenho|sem) interesse,? (nao|para de|pare de) (me )?(mande|mandar|chame|chamar|insista|insistir)",
    r"vou (denunciar|bloquear)",
    r"isso e spam",
]


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", sem_acento.lower())).strip()


def eh_opt_out(texto: str | None) -> bool:
    if not texto:
        return False
    limpo = normalizar(texto)
    palavras = limpo.split()
    if len(palavras) == 1 and palavras[0] in _PALAVRAS_SAIR:
        return True
    if 1 < len(palavras) <= 3 and _PALAVRAS_SAIR_FORTES.intersection(palavras):
        return True
    return any(re.search(frase, limpo) for frase in _FRASES_SAIR)


def extrair_texto(data: dict) -> str | None:
    message = data.get("message") or {}
    for caminho in (("conversation",), ("extendedTextMessage", "text"), ("imageMessage", "caption"), ("videoMessage", "caption")):
        valor = message
        for chave in caminho:
            valor = valor.get(chave) if isinstance(valor, dict) else None
        if isinstance(valor, str) and valor.strip():
            return valor
    return None


def _jids(data: dict) -> list[str]:
    key = data.get("key") or {}
    candidatos = [key.get("remoteJid"), key.get("remoteJidAlt"), key.get("senderPn"), data.get("senderPn")]
    return [j for j in candidatos if isinstance(j, str) and j.endswith("@s.whatsapp.net")]


def encontrar_lead(db: Session, data: dict) -> Lead | None:
    jids = _jids(data)
    if not jids:
        return None
    telefones = [f"+{j.split('@')[0].split(':')[0]}" for j in jids]
    return db.scalar(select(Lead).where(or_(Lead.whatsapp_jid.in_(jids), Lead.telefone.in_(telefones))).limit(1))


def adicionar_blacklist(db: Session, telefone: str, motivo: str) -> bool:
    if db.scalar(select(Blacklist.id).where(Blacklist.telefone == telefone)):
        return False
    try:
        with db.begin_nested():
            db.add(Blacklist(telefone=telefone, motivo=motivo))
    except IntegrityError:
        return False
    return True


# messageType da Evolution -> tipo no histórico. Os ignorados não são conversa (reação, edição, apagou…).
TIPOS = {
    "conversation": "texto",
    "extendedTextMessage": "texto",
    "audioMessage": "audio",
    "pttMessage": "audio",
    "imageMessage": "imagem",
    "videoMessage": "video",
    "documentMessage": "documento",
    "documentWithCaptionMessage": "documento",
    "stickerMessage": "figurinha",
    "locationMessage": "localizacao",
    "contactMessage": "contato",
}
IGNORADOS = {"reactionMessage", "protocolMessage", "senderKeyDistributionMessage", "messageContextInfo", "editedMessage"}
# A IA não entende estes: o lead vai para um humano.
TIPOS_PARA_HUMANO = {"audio", "imagem", "video", "documento", "localizacao", "contato"}


def tipo_da_mensagem(data: dict) -> str | None:
    """Tipo legível, ou None quando o evento não é uma mensagem de conversa."""
    tipo = data.get("messageType")
    if tipo is None:
        chaves = [k for k in (data.get("message") or {}) if k not in IGNORADOS]
        tipo = chaves[0] if chaves else None
    if tipo is None or tipo in IGNORADOS:
        return None
    return TIPOS.get(tipo, "outro")


def registrar_recebida(db: Session, payload: dict) -> Lead | None:
    """Mensagem que chegou de um contato. Guarda no histórico e decide o que fazer. Retorna o lead, se for um.

    Quando a IA deve responder, deixa `lead.responder_em` preenchido (quem chama agenda a resposta).
    """
    from app.services import agente, config_sistema

    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None  # ver registrar_propria
    tipo = tipo_da_mensagem(data)
    if tipo is None:
        return None
    lead = encontrar_lead(db, data)
    if lead is None:
        return None
    agora = datetime.now(timezone.utc)
    texto = extrair_texto(data) or ""
    db.add(
        Mensagem(lead_id=lead.id, numero_id=lead.numero_id, direcao=Direcao.ENTRADA, autor=Autor.LEAD,
                 tipo=tipo, texto=texto, evolution_message_id=key.get("id"))
    )
    if lead.respondeu_em is None:
        lead.respondeu_em = agora
    if lead.status == LeadStatus.ENVIADO:
        lead.status = LeadStatus.RESPONDEU

    if lead.status != LeadStatus.OPT_OUT and eh_opt_out(texto):
        lead.status = LeadStatus.OPT_OUT
        lead.ia_ativa = False
        lead.responder_em = None
        adicionar_blacklist(db, lead.telefone, "opt-out: pediu para não ser contatado")
        logger.info("opt-out do lead", extra={"lead_id": lead.id})
    elif not agente.pode_responder(lead):
        logger.info("lead respondeu (IA desligada para ele)", extra={"lead_id": lead.id, "status": lead.status})
    elif tipo in TIPOS_PARA_HUMANO:
        agente.passar_para_humano(db, lead, f"o lead mandou {tipo}, que a IA não entende")
    elif tipo == "texto":
        espera = int(config_sistema.valores(db)["ia_espera_segundos"])
        lead.responder_em = agora + timedelta(seconds=espera)
        logger.info("lead respondeu; resposta da IA agendada", extra={"lead_id": lead.id, "em_segundos": espera})
    return lead


def registrar_propria(db: Session, payload: dict) -> dict | None:
    """Mensagem enviada pelo nosso número. Se não foi o sistema, foi um humano (Chatwoot ou celular).

    Retorna os dados para a conferência adiada (o webhook do nosso próprio envio pode chegar antes de
    o id ficar gravado), ou None se já dá para ver que foi o sistema.
    """
    from app.services import agente

    data = payload.get("data") or {}
    key = data.get("key") or {}
    if not key.get("fromMe") or not key.get("id") or tipo_da_mensagem(data) is None:
        return None
    if agente.eh_do_sistema(db, key["id"]):
        return None
    if encontrar_lead(db, data) is None:
        return None  # conversa com quem não é lead (vendedor, grupo, contato pessoal)
    return {"id": key["id"], "data": data}


def confirmar_humano(db: Session, dados: dict) -> Lead | None:
    """Conferência adiada de uma mensagem nossa desconhecida. Retorna o lead se um humano assumiu."""
    from app.services import agente

    message_id, data = dados["id"], dados["data"]
    if agente.eh_do_sistema(db, message_id):
        return None
    lead = encontrar_lead(db, data)
    if lead is None:
        return None
    texto = extrair_texto(data) or ""
    # Um envio nosso que ainda não tinha o id gravado: mesmo lead, mesmo texto, há poucos minutos.
    recente = datetime.now(timezone.utc) - timedelta(minutes=3)
    nosso = db.scalar(
        select(Envio).where(Envio.lead_id == lead.id, Envio.texto == texto, Envio.created_at >= recente).order_by(Envio.id.desc()).limit(1)
    )
    if nosso is not None and texto:
        agente.marcar_como_do_sistema(db, message_id)
        return None
    db.add(
        Mensagem(lead_id=lead.id, numero_id=lead.numero_id, direcao=Direcao.SAIDA, autor=Autor.HUMANO,
                 tipo=tipo_da_mensagem(data) or "texto", texto=texto, evolution_message_id=message_id)
    )
    agente.marcar_como_do_sistema(db, message_id)  # não conferir de novo num reenvio do webhook
    if agente.pode_responder(lead) or lead.status != LeadStatus.HUMANO_ASSUMIU:
        agente.passar_para_humano(db, lead, "um humano respondeu pelo Chatwoot ou pelo celular")
    logger.info("humano assumiu o lead", extra={"lead_id": lead.id})
    return lead
