"""Quem já foi prospectado não recebe uma nova primeira mensagem.

O telefone do lead é único, mas o mesmo WhatsApp pode aparecer com dois telefones (celulares antigos
têm o identificador sem o 9). Por isso a conferência usa também o identificador do WhatsApp (jid).

Há três barreiras: na entrada da lista (buscas e importações), ao pôr leads numa campanha e,
a última, logo antes de mandar a abertura.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Envio, EnvioStatus, Lead, LeadStatus, Mensagem, NumeroStatus, WhatsappNumber

# Leads que nunca falaram com ninguém: os únicos que podem receber a primeira mensagem.
VIRGENS = (LeadStatus.NOVO, LeadStatus.NA_FILA)


def condicao_ja_prospectado():
    """Condição SQL sobre Lead: ele (ou outro cadastro com o mesmo WhatsApp) já foi abordado.

    As subconsultas usam select(...).exists() para receberem o filtro do cliente (exists() do Core escapa dele).
    """
    outro = aliased(Lead)
    return (
        Lead.enviado_em.is_not(None)
        | Lead.status.not_in(VIRGENS)
        | select(Envio.id).where(Envio.lead_id == Lead.id, Envio.status == EnvioStatus.ENVIADO).exists()
        | select(Mensagem.id).where(Mensagem.lead_id == Lead.id).exists()
        | (
            Lead.whatsapp_jid.is_not(None)
            & select(outro.id)
            .where(
                outro.whatsapp_jid == Lead.whatsapp_jid,
                outro.id != Lead.id,
                outro.enviado_em.is_not(None) | outro.status.not_in(VIRGENS),
            )
            .exists()
        )
        | select(WhatsappNumber.id)
        .where(WhatsappNumber.telefone == Lead.telefone, WhatsappNumber.status != NumeroStatus.REMOVIDO)
        .exists()
    )


def motivo_ja_prospectado(db: Session, lead: Lead) -> str | None:
    """Por que este lead não pode receber a primeira mensagem (None = pode)."""
    if lead.enviado_em is not None or db.scalar(
        select(Envio.id).where(Envio.lead_id == lead.id, Envio.status == EnvioStatus.ENVIADO).limit(1)
    ):
        return "já recebeu mensagem antes"
    if db.scalar(select(Mensagem.id).where(Mensagem.lead_id == lead.id).limit(1)):
        return "já tem conversa registrada"
    if lead.whatsapp_jid and db.scalar(
        select(Lead.id)
        .where(
            Lead.whatsapp_jid == lead.whatsapp_jid,
            Lead.id != lead.id,
            Lead.enviado_em.is_not(None) | Lead.status.not_in(VIRGENS),
        )
        .limit(1)
    ):
        return "o mesmo WhatsApp já foi prospectado em outro cadastro"
    if db.scalar(
        select(WhatsappNumber.id)
        .where(WhatsappNumber.telefone == lead.telefone, WhatsappNumber.status != NumeroStatus.REMOVIDO)
        .limit(1)
    ):
        return "é um dos números de prospecção"
    return None


def jids_ja_cadastrados(db: Session, jids: list[str]) -> dict[str, int]:
    """jid -> id de um lead que já usa esse WhatsApp."""
    resultado: dict[str, int] = {}
    for i in range(0, len(jids), 1000):
        lote = jids[i : i + 1000]
        resultado.update(db.execute(select(Lead.whatsapp_jid, Lead.id).where(Lead.whatsapp_jid.in_(lote))).all())
    return resultado
