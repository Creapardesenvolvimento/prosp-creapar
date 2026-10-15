"""Motor de disparo das mensagens de abertura.

Cada número tem o próprio relógio (`proximo_envio_em`). A cada ciclo, quem está liberado
manda no máximo uma mensagem, e o próximo envio é sorteado entre o intervalo mínimo e
máximo — esticado quando preciso para que o limite diário se espalhe pelo horário comercial.
Assim cada número envia em proporção ao seu limite da rampa.
"""

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.integrations import evolution
from app.integrations.evolution import EvolutionError
from app.models import (
    Autor,
    Blacklist,
    Direcao,
    Mensagem,
    Campanha,
    CampanhaMensagem,
    CampanhaStatus,
    Envio,
    EnvioStatus,
    EnvioTipo,
    Lead,
    LeadStatus,
    NumeroStatus,
    WhatsappNumber,
)
from app.models.campanhas import campanha_numeros
from app.services import campanhas, config_sistema
from app.services import numeros as numeros_svc
from app.services.agente import marcar_como_do_sistema
from app.services.agenda import como_utc, utc
from app.services.config_sistema import RegrasDisparo
from app.services.telefone import para_evolution

logger = logging.getLogger(__name__)

MAX_TENTATIVAS = 3
ESPERA_RESPOSTA = timedelta(hours=6)  # envios mais novos que isso não contam na taxa de resposta


def dentro_do_horario(regras: RegrasDisparo, agora: datetime) -> bool:
    local = como_utc(agora).astimezone(regras.tz)
    return local.weekday() in regras.dias and regras.inicio <= local.time() < regras.fim


def segundos_ate_o_fim(regras: RegrasDisparo, agora: datetime) -> float:
    local = como_utc(agora).astimezone(regras.tz)
    fim = local.replace(hour=regras.fim.hour, minute=regras.fim.minute, second=0, microsecond=0)
    return max((fim - local).total_seconds(), 0.0)


def proximo_intervalo(regras: RegrasDisparo, numero: WhatsappNumber, agora: datetime, rng: random.Random) -> float:
    """Segundos até o próximo envio deste número."""
    sorteado = rng.uniform(regras.intervalo_min_seg, regras.intervalo_max_seg)
    restantes = numero.limite_diario_atual - numero.envios_hoje
    if restantes <= 0:
        return sorteado
    # Espalha o que falta do limite pelo resto do expediente, com variação para não ficar regular.
    # Dividir por restantes + 1 garante que o último envio ainda cabe antes do fim, mesmo com +20%.
    ritmo = segundos_ate_o_fim(regras, agora) / (restantes + 1)
    return max(sorteado, ritmo * rng.uniform(0.8, 1.2))


def numeros_aptos(db: Session, agora: datetime) -> list[WhatsappNumber]:
    em_campanha_ativa = (
        select(campanha_numeros.c.numero_id)
        .join(Campanha, Campanha.id == campanha_numeros.c.campanha_id)
        .where(Campanha.status == CampanhaStatus.ATIVA)
    )
    numeros = db.scalars(
        select(WhatsappNumber)
        .where(
            WhatsappNumber.status == NumeroStatus.CONECTADO,
            WhatsappNumber.id.in_(em_campanha_ativa),
            WhatsappNumber.envios_hoje < WhatsappNumber.limite_diario_atual,
        )
        .order_by(WhatsappNumber.id)
    ).all()
    return [n for n in numeros if n.proximo_envio_em is None or como_utc(n.proximo_envio_em) <= agora]


def reservar_lead(db: Session, numero: WhatsappNumber) -> Lead | None:
    """Pega o próximo lead da fila das campanhas ativas deste número e o amarra a ele.

    A reserva é um UPDATE condicional: se outro processo pegou o mesmo lead, tenta o próximo.
    É o que garante que um lead nunca recebe a primeira mensagem de dois números.
    """
    campanhas_do_numero = (
        select(campanha_numeros.c.campanha_id)
        .join(Campanha, Campanha.id == campanha_numeros.c.campanha_id)
        .where(campanha_numeros.c.numero_id == numero.id, Campanha.status == CampanhaStatus.ATIVA)
    )
    candidatos = db.scalars(
        select(Lead.id)
        .where(
            Lead.campanha_id.in_(campanhas_do_numero),
            Lead.status == LeadStatus.NA_FILA,
            Lead.numero_id.is_(None),
            Lead.tentativas_envio < MAX_TENTATIVAS,
        )
        .order_by(Lead.id)
        .limit(5)
    ).all()
    for lead_id in candidatos:
        result = db.execute(
            update(Lead)
            .where(Lead.id == lead_id, Lead.numero_id.is_(None), Lead.status == LeadStatus.NA_FILA)
            .values(numero_id=numero.id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            db.flush()
            lead = db.get(Lead, lead_id)
            db.refresh(lead)
            return lead
    return None


def escolher_mensagem(db: Session, campanha: Campanha, numero: WhatsappNumber, rng: random.Random) -> CampanhaMensagem:
    ativas = [m for m in campanha.mensagens if m.ativa]
    if len(ativas) > 1:
        ultima = db.scalar(
            select(Envio.mensagem_id).where(Envio.numero_id == numero.id).order_by(Envio.id.desc()).limit(1)
        )
        ativas = [m for m in ativas if m.id != ultima] or ativas
    return rng.choice(ativas)


def enviar_abertura(db: Session, numero: WhatsappNumber, lead: Lead, agora: datetime, rng: random.Random) -> Envio | None:
    if db.scalar(select(Blacklist.id).where(Blacklist.telefone == lead.telefone)):
        lead.status = LeadStatus.OPT_OUT
        logger.info("lead na blacklist retirado da fila", extra={"lead_id": lead.id})
        return None
    campanha = db.get(Campanha, lead.campanha_id)
    mensagem = escolher_mensagem(db, campanha, numero, rng)
    envio = Envio(
        lead_id=lead.id,
        campanha_id=campanha.id,
        numero_id=numero.id,
        mensagem_id=mensagem.id,
        tipo=EnvioTipo.ABERTURA,
        texto=campanhas.renderizar(mensagem.texto, lead),
        status=EnvioStatus.PENDENTE,
    )
    db.add(envio)
    db.flush()
    try:
        resposta = evolution.get_client(numero.server).send_text(
            numero.instance_name, para_evolution(lead.telefone), envio.texto
        )
    except EvolutionError as exc:
        envio.status = EnvioStatus.FALHOU
        envio.erro = str(exc)
        lead.numero_id = None  # volta para a fila geral
        lead.tentativas_envio += 1
        logger.error(
            "falha no envio", extra={"numero_id": numero.id, "lead_id": lead.id, "tentativa": lead.tentativas_envio, "erro": str(exc)}
        )
        return envio
    envio.status = EnvioStatus.ENVIADO
    envio.enviado_em = agora
    envio.evolution_message_id = (resposta.get("key") or {}).get("id")
    marcar_como_do_sistema(db, envio.evolution_message_id)
    db.add(
        Mensagem(lead_id=lead.id, numero_id=numero.id, direcao=Direcao.SAIDA, autor=Autor.SISTEMA,
                 texto=envio.texto, evolution_message_id=envio.evolution_message_id)
    )
    lead.status = LeadStatus.ENVIADO
    lead.enviado_em = agora
    numero.envios_hoje += 1
    logger.info(
        "mensagem de abertura enviada",
        extra={"numero_id": numero.id, "lead_id": lead.id, "campanha_id": campanha.id, "envios_hoje": numero.envios_hoje},
    )
    return envio


def conferir_taxa_de_resposta(db: Session, numero: WhatsappNumber, regras: RegrasDisparo, agora: datetime) -> bool:
    """Pausa o número se a taxa de resposta dos últimos envios ficou abaixo do mínimo. Retorna True se pausou."""
    if regras.taxa_minima <= 0:
        return False
    ultimos = db.execute(
        select(Lead.respondeu_em)
        .join(Envio, Envio.lead_id == Lead.id)
        .where(
            Envio.numero_id == numero.id,
            Envio.tipo == EnvioTipo.ABERTURA,
            Envio.status == EnvioStatus.ENVIADO,
            Envio.enviado_em <= agora - ESPERA_RESPOSTA,
        )
        .order_by(Envio.enviado_em.desc())
        .limit(regras.janela_envios)
    ).all()
    if len(ultimos) < regras.janela_envios:
        return False
    taxa = 100 * sum(1 for (respondeu,) in ultimos if respondeu) / len(ultimos)
    if taxa >= regras.taxa_minima:
        return False
    numero.status = NumeroStatus.PAUSADO
    numero.motivo_pausa = f"taxa de resposta baixa ({taxa:.0f}% nos últimos {len(ultimos)} envios)"
    logger.warning("número pausado por taxa de resposta baixa", extra={"numero_id": numero.id, "taxa": round(taxa, 1)})
    return True


def ciclo(db: Session, agora: datetime | None = None, rng: random.Random | None = None) -> int:
    """Uma rodada do disparo. Retorna quantas mensagens saíram. Faz commit a cada envio."""
    agora = utc(agora or datetime.now(timezone.utc))  # o SQLite descarta o fuso ao gravar
    rng = rng or random.Random()
    if numeros_svc.garantir_virada(db, agora):
        db.commit()
    regras = config_sistema.regras_disparo(db)
    if not dentro_do_horario(regras, agora):
        return 0
    enviados = 0
    for numero in numeros_aptos(db, agora):
        if conferir_taxa_de_resposta(db, numero, regras, agora):
            db.commit()
            continue
        lead = reservar_lead(db, numero)
        if lead is None:
            continue
        envio = enviar_abertura(db, numero, lead, agora, rng)
        if envio is not None and envio.status == EnvioStatus.ENVIADO:
            enviados += 1
            numero.proximo_envio_em = agora + timedelta(seconds=proximo_intervalo(regras, numero, agora, rng))
        elif envio is not None:
            numero.proximo_envio_em = agora + timedelta(seconds=regras.intervalo_min_seg)
        db.commit()
    return enviados


def resumo_fila(db: Session) -> dict:
    return {
        "na_fila": db.scalar(select(func.count()).select_from(Lead).where(Lead.status == LeadStatus.NA_FILA)) or 0,
    }
