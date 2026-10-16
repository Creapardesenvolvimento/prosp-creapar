"""Follow-up automático para quem recebeu a primeira mensagem e não respondeu.

O passo k (1 = primeiro) vence `dias[0]` dias depois da abertura; os seguintes vencem a diferença
entre os dias a partir do follow-up anterior — assim, um lead antigo não recebe dois de uma vez.
Quem responde sai do follow-up sozinho (deixa de estar "enviado"). Sem resposta alguns dias
depois do último passo, o lead vira "sem resposta".

O envio é feito pelo motor de disparo, que já cuida do horário, do limite diário e do ritmo de cada número.
"""

import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations import evolution
from app.integrations.evolution import EvolutionError
from app.models import (
    Autor,
    Campanha,
    CampanhaFollowup,
    CampanhaStatus,
    Direcao,
    Envio,
    EnvioStatus,
    EnvioTipo,
    Lead,
    LeadStatus,
    Mensagem,
    WhatsappNumber,
)
from app.services import campanhas, config_sistema
from app.services.agenda import como_utc, utc
from app.services.telefone import para_evolution

logger = logging.getLogger(__name__)

ESPERA_FINAL = timedelta(days=3)  # depois do último follow-up, quanto esperar até marcar "sem resposta"

# Textos de fábrica por papel do passo. {atendente}, {marca} e {economia} vêm de Configurações.
PADROES = {
    "lembrete": [
        "Oi, tudo bem? Conseguiu ver minha mensagem? 🙂",
        "Oi! Passando só pra saber se você viu minha mensagem sobre a {empresa}.",
    ],
    "retomada": [
        "Oi, aqui é a {atendente}, da {marca}. Sei que a rotina é corrida, então resumo: ajudamos empresas como a "
        "{empresa} a economizar {economia}. Faz sentido conversarmos uns 15 minutinhos?",
        "Oi! Retomando rapidinho: a {marca} tem ajudado empresas como a {empresa} a economizar {economia}. "
        "Posso te mostrar como numa conversa rápida?",
    ],
    "despedida": [
        "Oi! Não quero incomodar, então essa é minha última mensagem. Se em algum momento quiser saber como "
        "economizar {economia}, é só me chamar aqui. Abraço!",
        "Oi, vou encerrar meu contato por aqui pra não atrapalhar sua rotina. Se fizer sentido no futuro, "
        "é só responder esta mensagem. Até mais!",
    ],
}


def regras(db: Session) -> tuple[bool, list[int]]:
    v = config_sistema.valores(db)
    return bool(v["followup_ativo"]), config_sistema.parse_dias_followup(v["followup_dias"])


def papel_do_passo(passo: int, total: int) -> str:
    if passo == total and total > 1:
        return "despedida"
    return "lembrete" if passo == 1 else "retomada"


def textos_de_fabrica(db: Session, passo: int, total: int) -> list[str]:
    v = config_sistema.valores(db)
    return [
        t.replace("{atendente}", v["atendente_nome"]).replace("{marca}", v["empresa_nome"]).replace("{economia}", v["economia_texto"])
        for t in PADROES[papel_do_passo(passo, total)]
    ]


def textos_do_passo(db: Session, campanha: Campanha, passo: int, total: int) -> list[str]:
    proprios = [f.texto for f in campanha.followups if f.passo == passo]
    return proprios or textos_de_fabrica(db, passo, total)


def vencimento(lead: Lead, dias: list[int]) -> datetime | None:
    """Quando vence o próximo follow-up deste lead (None se não há mais passos)."""
    k = lead.followups_enviados
    if k >= len(dias) or lead.enviado_em is None:
        return None
    if k == 0 or lead.ultimo_followup_em is None:
        return como_utc(lead.enviado_em) + timedelta(days=dias[0])
    return como_utc(lead.ultimo_followup_em) + timedelta(days=dias[k] - dias[k - 1])


def lead_para_followup(db: Session, numero: WhatsappNumber, agora: datetime, dias: list[int]) -> Lead | None:
    """O lead deste número com o follow-up vencido há mais tempo (das campanhas ativas)."""
    candidatos = db.scalars(
        select(Lead)
        .join(Campanha, Campanha.id == Lead.campanha_id)
        .where(
            Lead.numero_id == numero.id,
            Lead.status == LeadStatus.ENVIADO,
            Lead.ia_ativa,
            Lead.followups_enviados < len(dias),
            Lead.enviado_em.is_not(None),
            Lead.enviado_em <= utc(agora - timedelta(days=dias[0])),
            Campanha.status == CampanhaStatus.ATIVA,
        )
        .order_by(Lead.enviado_em)
        .limit(200)
    ).all()
    vencidos = [(v, lead) for lead in candidatos if (v := vencimento(lead, dias)) is not None and v <= agora]
    return min(vencidos, key=lambda par: (par[0], par[1].id))[1] if vencidos else None


def enviar_followup(
    db: Session, numero: WhatsappNumber, lead: Lead, agora: datetime, rng: random.Random, dias: list[int]
) -> Envio:
    from app.services.agente import marcar_como_do_sistema

    passo = lead.followups_enviados + 1
    campanha = db.get(Campanha, lead.campanha_id)
    texto = campanhas.renderizar(rng.choice(textos_do_passo(db, campanha, passo, len(dias))), lead)
    envio = Envio(
        lead_id=lead.id, campanha_id=campanha.id, numero_id=numero.id, tipo=EnvioTipo.FOLLOW_UP,
        texto=texto, status=EnvioStatus.PENDENTE,
    )
    db.add(envio)
    db.flush()
    # Falhando ou não, o passo conta: não fica tentando o mesmo lead a cada ciclo.
    lead.followups_enviados = passo
    lead.ultimo_followup_em = agora
    try:
        resposta = evolution.get_client(numero.server).send_text(numero.instance_name, para_evolution(lead.telefone), texto)
    except EvolutionError as exc:
        envio.status, envio.erro = EnvioStatus.FALHOU, str(exc)
        logger.error("falha no follow-up", extra={"numero_id": numero.id, "lead_id": lead.id, "passo": passo, "erro": str(exc)})
        return envio
    envio.status = EnvioStatus.ENVIADO
    envio.enviado_em = agora
    envio.evolution_message_id = (resposta.get("key") or {}).get("id")
    marcar_como_do_sistema(db, envio.evolution_message_id)
    db.add(
        Mensagem(lead_id=lead.id, numero_id=numero.id, direcao=Direcao.SAIDA, autor=Autor.SISTEMA,
                 texto=texto, evolution_message_id=envio.evolution_message_id)
    )
    numero.envios_hoje += 1
    logger.info("follow-up enviado", extra={"numero_id": numero.id, "lead_id": lead.id, "passo": passo})
    return envio


def encerrar_sem_resposta(db: Session, agora: datetime, dias: list[int]) -> int:
    """Quem recebeu todos os follow-ups e seguiu calado vira "sem resposta"."""
    leads = db.scalars(
        select(Lead).where(
            Lead.status == LeadStatus.ENVIADO,
            Lead.followups_enviados >= len(dias),
            Lead.ultimo_followup_em <= utc(agora - ESPERA_FINAL),
        )
    ).all()
    for lead in leads:
        lead.status = LeadStatus.SEM_RESPOSTA
    if leads:
        logger.info("leads sem resposta depois do follow-up", extra={"quantidade": len(leads)})
    return len(leads)
