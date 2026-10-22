"""Motor de disparo das mensagens de abertura.

Cada número tem o próprio relógio (`proximo_envio_em`). A cada ciclo, quem está liberado
manda no máximo uma mensagem, e o próximo envio é sorteado entre o intervalo mínimo e
máximo — esticado quando preciso para que o limite diário se espalhe pelo horário comercial.
Assim cada número envia em proporção ao seu limite da rampa.
"""

import logging
import math
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
    CampanhaModo,
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
from app.services import alertas, campanhas, config_sistema, followup, historico
from app.services import numeros as numeros_svc
from app.services.agente import marcar_como_do_sistema
from app.services.agenda import como_utc, utc
from app.services.config_sistema import RegrasDisparo
from app.services.telefone import para_evolution

logger = logging.getLogger(__name__)

MAX_TENTATIVAS = 3
MAX_PULOS = 10  # leads tirados da fila seguidos que um número pula num ciclo
ESPERA_RESPOSTA = timedelta(hours=6)  # envios mais novos que isso não contam na taxa de resposta


def dentro_do_horario(regras: RegrasDisparo, agora: datetime) -> bool:
    local = como_utc(agora).astimezone(regras.tz)
    return local.weekday() in regras.dias and regras.inicio <= local.time() < regras.fim


def segundos_ate_o_fim(regras: RegrasDisparo, agora: datetime) -> float:
    local = como_utc(agora).astimezone(regras.tz)
    fim = local.replace(hour=regras.fim.hour, minute=regras.fim.minute, second=0, microsecond=0)
    return max((fim - local).total_seconds(), 0.0)


def inicio_do_dia(regras: RegrasDisparo, agora: datetime) -> datetime:
    local = como_utc(agora).astimezone(regras.tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def horas_de_disparo(regras: RegrasDisparo) -> float:
    return (regras.fim.hour * 60 + regras.fim.minute - regras.inicio.hour * 60 - regras.inicio.minute) / 60


def meta_por_hora(regras: RegrasDisparo, meta: int) -> int:
    """Teto de primeiras mensagens por hora, com 15% de folga para recuperar atraso."""
    return math.ceil(meta * 1.15 / max(horas_de_disparo(regras), 1))


def campanhas_super_ativas(db: Session) -> set[int]:
    return set(
        db.scalars(select(Campanha.id).where(Campanha.status == CampanhaStatus.ATIVA, Campanha.modo == CampanhaModo.SUPER))
    )


def contar_envios(db: Session, desde: datetime, tipos=(EnvioTipo.ABERTURA,), campanhas: set[int] | None = None) -> int:
    """campanhas: conta só os envios delas (None = todas)."""
    consulta = (
        select(func.count())
        .select_from(Envio)
        .where(Envio.tipo.in_(list(tipos)), Envio.status == EnvioStatus.ENVIADO, Envio.enviado_em >= utc(desde))
    )
    if campanhas is not None:
        consulta = consulta.where(Envio.campanha_id.in_(campanhas))
    return db.scalar(consulta) or 0


def envios_por_numero(db: Session, desde: datetime) -> dict[int, int]:
    return dict(
        db.execute(
            select(Envio.numero_id, func.count())
            .where(
                Envio.tipo.in_([EnvioTipo.ABERTURA, EnvioTipo.FOLLOW_UP]),
                Envio.status == EnvioStatus.ENVIADO,
                Envio.enviado_em >= utc(desde),
            )
            .group_by(Envio.numero_id)
        ).all()
    )


def proximo_intervalo(
    regras: RegrasDisparo, numero: WhatsappNumber, agora: datetime, rng: random.Random, cota: int | None = None
) -> float:
    """Segundos até o próximo envio deste número. cota: quanto da meta do dia ainda cabe a cada número."""
    sorteado = rng.uniform(regras.intervalo_min_seg, regras.intervalo_max_seg)
    restantes = numero.limite_diario_atual - numero.envios_hoje
    if cota is not None:
        restantes = min(restantes, cota)
    if restantes <= 0:
        return sorteado
    # Espalha o que falta do limite pelo resto do expediente, com variação para não ficar regular.
    # Dividir por restantes + 1 garante que o último envio ainda cabe antes do fim, mesmo com +20%.
    ritmo = segundos_ate_o_fim(regras, agora) / (restantes + 1)
    return max(sorteado, ritmo * rng.uniform(0.8, 1.2))


def numeros_com_capacidade(db: Session) -> list[WhatsappNumber]:
    """Conectados, em campanha ativa e ainda abaixo do limite de hoje (liberados agora ou não)."""
    em_campanha_ativa = (
        select(campanha_numeros.c.numero_id)
        .join(Campanha, Campanha.id == campanha_numeros.c.campanha_id)
        .where(Campanha.status == CampanhaStatus.ATIVA)
    )
    return db.scalars(
        select(WhatsappNumber)
        .where(
            WhatsappNumber.status == NumeroStatus.CONECTADO,
            WhatsappNumber.id.in_(em_campanha_ativa),
            WhatsappNumber.envios_hoje < WhatsappNumber.limite_diario_atual,
        )
        .order_by(WhatsappNumber.id)
    ).all()


def numeros_aptos(db: Session, agora: datetime) -> list[WhatsappNumber]:
    return [n for n in numeros_com_capacidade(db) if n.proximo_envio_em is None or como_utc(n.proximo_envio_em) <= agora]


def reservar_lead(db: Session, numero: WhatsappNumber, sem_campanhas: set[int] | None = None) -> Lead | None:
    """Pega o próximo lead da fila das campanhas ativas deste número e o amarra a ele.

    A reserva é um UPDATE condicional: se outro processo pegou o mesmo lead, tenta o próximo.
    É o que garante que um lead nunca recebe a primeira mensagem de dois números.
    sem_campanhas: campanhas que não podem receber abertura agora (super prospecção com a meta batida).
    """
    campanhas_do_numero = (
        select(campanha_numeros.c.campanha_id)
        .join(Campanha, Campanha.id == campanha_numeros.c.campanha_id)
        .where(campanha_numeros.c.numero_id == numero.id, Campanha.status == CampanhaStatus.ATIVA)
    )
    if sem_campanhas:
        campanhas_do_numero = campanhas_do_numero.where(Campanha.id.not_in(sem_campanhas))
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


def enviar_abertura(
    db: Session, numero: WhatsappNumber, lead: Lead, agora: datetime, rng: random.Random, conferir_historico: bool = True
) -> Envio | None:
    """conferir_historico=False só para o lead de teste, que é reiniciado de propósito."""
    if db.scalar(select(Blacklist.id).where(Blacklist.telefone == lead.telefone)):
        lead.status = LeadStatus.OPT_OUT
        logger.info("lead na blacklist retirado da fila", extra={"lead_id": lead.id})
        return None
    motivo = historico.motivo_ja_prospectado(db, lead) if conferir_historico else None
    if motivo:
        lead.status, lead.numero_id = LeadStatus.JA_PROSPECTADO, None
        logger.info("lead já prospectado retirado da fila", extra={"lead_id": lead.id, "motivo": motivo})
        return None
    campanha = db.get(Campanha, lead.campanha_id)
    mensagem = escolher_mensagem(db, campanha, numero, rng)
    envio = Envio(
        lead_id=lead.id,
        campanha_id=campanha.id,
        numero_id=numero.id,
        mensagem_id=mensagem.id,
        tipo=EnvioTipo.ABERTURA,
        texto=campanhas.renderizar(mensagem.texto, lead, rng),
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


def conferir_falhas(db: Session, numero: WhatsappNumber, falhas_seguidas: int) -> bool:
    """Pausa o número se os últimos envios falharam todos (chip bloqueado ou sessão caída). Retorna True se pausou."""
    if falhas_seguidas <= 0:
        return False
    ultimos = db.scalars(
        select(Envio.status)
        .where(Envio.numero_id == numero.id, Envio.tipo.in_([EnvioTipo.ABERTURA, EnvioTipo.FOLLOW_UP]))
        .order_by(Envio.id.desc())
        .limit(falhas_seguidas)
    ).all()
    if len(ultimos) < falhas_seguidas or any(status != EnvioStatus.FALHOU for status in ultimos):
        return False
    numero.status = NumeroStatus.PAUSADO
    numero.motivo_pausa = f"{falhas_seguidas} falhas seguidas no envio"
    logger.warning("número pausado por falhas seguidas", extra={"numero_id": numero.id, "falhas": falhas_seguidas})
    return True


def ciclo(db: Session, agora: datetime | None = None, rng: random.Random | None = None) -> int:
    """Uma rodada do disparo. Retorna quantas mensagens saíram. Faz commit a cada envio."""
    agora = utc(agora or datetime.now(timezone.utc))  # o SQLite descarta o fuso ao gravar
    rng = rng or random.Random()
    if numeros_svc.garantir_virada(db, agora):
        db.commit()
    followup_ativo, dias_followup = followup.regras(db)
    if followup.encerrar_sem_resposta(db, agora, dias_followup, followup_ativo):
        db.commit()
    regras = config_sistema.regras_disparo(db)
    if not dentro_do_horario(regras, agora):
        return 0
    # Super prospecção: a meta do dia e o máximo por hora valem só para as campanhas desse tipo e os
    # números ligados a elas. As campanhas normais seguem só os limites de cada número (rampa e intervalos).
    volume = config_sistema.regras_volume(db)
    super_ids = campanhas_super_ativas(db)
    numeros_super = numeros_svc.ids_em_super_prospeccao(db) if super_ids else set()
    meta = volume.meta_diaria if super_ids else 0
    # Meta do dia: as primeiras mensagens das campanhas super somadas. Follow-up não conta na meta
    # (mas conta no limite de cada número).
    aberturas_hoje = contar_envios(db, inicio_do_dia(regras, agora), campanhas=super_ids) if meta else 0
    aberturas_hora = contar_envios(db, agora - timedelta(hours=1), campanhas=super_ids) if meta else 0
    teto_hora = meta_por_hora(regras, meta) if meta else 0
    cota = None
    if meta:
        # O que falta da meta, repartido entre os números da super que ainda podem enviar hoje.
        com_capacidade = [n for n in numeros_com_capacidade(db) if n.id in numeros_super]
        cota = math.ceil(max(meta - aberturas_hoje, 0) / max(len(com_capacidade), 1))
    na_hora = envios_por_numero(db, agora - timedelta(hours=1)) if numeros_super else {}

    enviados = 0
    for numero in numeros_aptos(db, agora):
        eh_super = numero.id in numeros_super
        if conferir_taxa_de_resposta(db, numero, regras, agora):
            alertas.avisar_chip(db, numero, "pausado", numero.motivo_pausa, agora)
            db.commit()
            continue
        if eh_super and na_hora.get(numero.id, 0) >= volume.limite_hora_numero:
            continue  # já mandou o máximo da hora; volta quando a janela andar
        # Follow-up vencido tem a vez: quem já recebeu a abertura deste número vem antes de lead novo.
        lead = followup.lead_para_followup(db, numero, agora, dias_followup) if followup_ativo else None
        if lead is not None:
            envio = followup.enviar_followup(db, numero, lead, agora, rng, dias_followup)
        else:
            # Meta do dia (ou da hora) batida: as campanhas super param; as normais do número seguem.
            meta_batida = bool(meta) and (aberturas_hoje >= meta or aberturas_hora >= teto_hora)
            # Lead tirado da fila (blacklist, já prospectado) não gasta a vez do número: tenta o próximo.
            for _ in range(MAX_PULOS):
                lead = reservar_lead(db, numero, sem_campanhas=super_ids if meta_batida else None)
                envio = enviar_abertura(db, numero, lead, agora, rng) if lead is not None else None
                if lead is None or envio is not None:
                    break
                db.commit()
            if lead is None or envio is None:
                db.commit()
                continue
            if envio is not None and envio.status == EnvioStatus.ENVIADO and lead.campanha_id in super_ids:
                aberturas_hoje += 1
                aberturas_hora += 1
        if envio is not None and envio.status == EnvioStatus.ENVIADO:
            enviados += 1
            numero.proximo_envio_em = agora + timedelta(
                seconds=proximo_intervalo(regras, numero, agora, rng, cota if eh_super else None)
            )
        elif envio is not None:
            numero.proximo_envio_em = agora + timedelta(seconds=regras.intervalo_min_seg)
            if conferir_falhas(db, numero, volume.falhas_seguidas):
                alertas.avisar_chip(db, numero, "pausado", numero.motivo_pausa, agora)
        db.commit()
    return enviados


def resumo_fila(db: Session) -> dict:
    return {
        "na_fila": db.scalar(select(func.count()).select_from(Lead).where(Lead.status == LeadStatus.NA_FILA)) or 0,
    }
