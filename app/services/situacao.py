"""Situação de cada empresa/lead na prospecção, em linguagem do time.

O status do lead é a fonte; a situação junta o que importa para decidir o próximo passo
(ex.: "enviado" vira "aguardando resposta" ou "em follow-up", conforme os follow-ups já enviados).
"""

from dataclasses import dataclass

from sqlalchemy import and_, false

from app.models import Empresa, Lead, LeadStatus


@dataclass(frozen=True)
class Situacao:
    chave: str
    rotulo: str
    contatado: bool  # já recebeu mensagem nossa (ou falou com a gente)


SITUACOES = [
    Situacao("nao_contatado", "Não contatado", False),
    Situacao("na_fila", "Na fila de disparo", False),
    Situacao("aguardando", "Aguardando resposta", True),
    Situacao("followup", "Em follow-up", True),
    Situacao("nem_respondeu", "Nem respondeu", True),
    Situacao("conversa", "Em conversa", True),
    Situacao("humano", "Com o time", True),
    Situacao("reuniao", "Reunião agendada", True),
    Situacao("sem_interesse", "Sem interesse", True),
    Situacao("desqualificado", "Desqualificado", True),
    Situacao("opt_out", "Pediu para não receber", True),
    Situacao("ja_prospectado", "Duplicado de quem já foi prospectado", True),
    Situacao("sem_whatsapp", "Sem WhatsApp / descartada", False),
    Situacao("processando", "Conferindo WhatsApp", False),
]
POR_CHAVE = {s.chave: s for s in SITUACOES}

_POR_STATUS = {
    LeadStatus.NOVO: "nao_contatado",
    LeadStatus.NA_FILA: "na_fila",
    LeadStatus.SEM_RESPOSTA: "nem_respondeu",
    LeadStatus.RESPONDEU: "conversa",
    LeadStatus.EM_CONVERSA: "conversa",
    LeadStatus.HUMANO_ASSUMIU: "humano",
    LeadStatus.REUNIAO_AGENDADA: "reuniao",
    LeadStatus.SEM_INTERESSE: "sem_interesse",
    LeadStatus.DESQUALIFICADO: "desqualificado",
    LeadStatus.OPT_OUT: "opt_out",
    LeadStatus.JA_PROSPECTADO: "ja_prospectado",
}


def chave_da_situacao(status: str | None, followups: int = 0, descartada: bool = False) -> str:
    if status is None:
        return "sem_whatsapp" if descartada else "processando"
    if status == LeadStatus.ENVIADO:
        return "followup" if followups else "aguardando"
    return _POR_STATUS.get(status, "nao_contatado")


def situacao(lead: Lead | None, empresa: Empresa | None = None) -> Situacao:
    if lead is None:
        return POR_CHAVE[chave_da_situacao(None, descartada=bool(empresa and empresa.descarte))]
    return POR_CHAVE[chave_da_situacao(lead.status, lead.followups_enviados or 0)]


def condicao(chave: str):
    """Filtro SQL da situação, para consultas com Empresa e Lead (lead pode faltar: outer join)."""
    if chave == "sem_whatsapp":
        return and_(Lead.id.is_(None), Empresa.descarte.is_not(None))
    if chave == "processando":
        return and_(Lead.id.is_(None), Empresa.descarte.is_(None))
    if chave == "aguardando":
        return and_(Lead.status == LeadStatus.ENVIADO, Lead.followups_enviados == 0)
    if chave == "followup":
        return and_(Lead.status == LeadStatus.ENVIADO, Lead.followups_enviados > 0)
    status = [s for s, c in _POR_STATUS.items() if c == chave]
    return Lead.status.in_(status) if status else false()
