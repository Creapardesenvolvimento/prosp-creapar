"""Super prospecção: o painel de volume (meta do dia, capacidade dos números, estoque de leads).

O disparo em si continua no motor de sempre (app.services.disparo); aqui só se mede e se projeta.
"""

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Campanha,
    CampanhaStatus,
    EnvioTipo,
    Lead,
    LeadStatus,
    NumeroStatus,
    Reuniao,
    WhatsappNumber,
)
from app.models.campanhas import campanha_numeros
from app.services import config_sistema, disparo, followup
from app.services.agenda import como_utc, utc
from app.services.rampa import Rampa, dia_de_rampa, limite_para_dia

FORA_DE_USO = (NumeroStatus.BANIDO, NumeroStatus.REMOVIDO)
RESERVA = 1.2  # 20% de números a mais, para cobrir os que forem pausados ou caírem
ESTOQUE_MINIMO_DIAS = 1.5
HORIZONTE_DIAS = 60


def numeros_da_prospeccao(db: Session) -> list[WhatsappNumber]:
    """Números ligados a alguma campanha ativa (em qualquer situação, menos removidos)."""
    em_campanha_ativa = (
        select(campanha_numeros.c.numero_id)
        .join(Campanha, Campanha.id == campanha_numeros.c.campanha_id)
        .where(Campanha.status == CampanhaStatus.ATIVA)
    )
    return list(
        db.scalars(
            select(WhatsappNumber)
            .where(WhatsappNumber.id.in_(em_campanha_ativa), WhatsappNumber.status != NumeroStatus.REMOVIDO)
            .order_by(WhatsappNumber.id)
        )
    )


class ProspeccaoError(Exception):
    pass


def numeros_cadastrados(db: Session) -> list[WhatsappNumber]:
    """Todos os números ainda em uso (qualquer situação, menos banidos e removidos)."""
    return list(db.scalars(select(WhatsappNumber).where(WhatsappNumber.status.not_in(FORA_DE_USO)).order_by(WhatsappNumber.id)))


def puxar_numeros(db: Session) -> tuple[int, int]:
    """Coloca todos os números cadastrados em todas as campanhas ativas. Retorna (campanhas, números novos nelas).

    Quem não está conectado entra também: o disparo só usa os conectados, então ele passa a enviar
    sozinho assim que conectar.
    """
    ativas = db.scalars(select(Campanha).where(Campanha.status == CampanhaStatus.ATIVA)).all()
    if not ativas:
        raise ProspeccaoError("Nenhuma campanha ativa para receber os números. Ative uma campanha primeiro.")
    disponiveis = numeros_cadastrados(db)
    adicionados = set()
    for campanha in ativas:
        ja = {n.id for n in campanha.numeros}
        for numero in disponiveis:
            if numero.id not in ja:
                campanha.numeros.append(numero)
                adicionados.add(numero.id)
    db.flush()
    return len(ativas), len(adicionados)


def capacidade_no_dia(conectados: list[WhatsappNumber], rampa: Rampa, agora: datetime, dias_a_frente: int) -> int:
    """Soma dos limites dos números daqui a N dias, se continuarem conectados (a rampa já vem com o teto)."""
    tz = get_settings().timezone
    return sum(
        limite_para_dia(dia_de_rampa(n.data_ativacao, agora, tz) + dias_a_frente, rampa)
        for n in conectados
        if n.data_ativacao is not None
    )


def dia_em_que_alcanca(conectados: list[WhatsappNumber], rampa: Rampa, agora: datetime, alvo: int) -> int | None:
    """Em quantos dias os números atuais, subindo pela rampa, aguentam o alvo. None: nunca, faltam números."""
    if alvo <= 0:
        return 0
    for k in range(HORIZONTE_DIAS + 1):
        if capacidade_no_dia(conectados, rampa, agora, k) >= alvo:
            return k
    return None


def _taxas_por_numero(db: Session) -> dict[int, tuple[int, int]]:
    """numero_id -> (primeiras mensagens enviadas, respostas)."""
    linhas = db.execute(
        select(Lead.numero_id, func.count(), func.count(Lead.respondeu_em))
        .where(Lead.numero_id.is_not(None), Lead.enviado_em.is_not(None))
        .group_by(Lead.numero_id)
    ).all()
    return {numero_id: (enviados, respostas) for numero_id, enviados, respostas in linhas}


def painel(db: Session, agora: datetime | None = None) -> dict:
    agora = utc(agora or datetime.now(timezone.utc))
    regras = config_sistema.regras_disparo(db)
    volume = config_sistema.regras_volume(db)
    rampa = config_sistema.rampa(db)
    meta = volume.meta_diaria
    meia_noite = disparo.inicio_do_dia(regras, agora)
    hora_atras = agora - timedelta(hours=1)

    aberturas_hoje = disparo.contar_envios(db, meia_noite)
    hoje = {
        "aberturas": aberturas_hoje,
        "followups": disparo.contar_envios(db, meia_noite, (EnvioTipo.FOLLOW_UP,)),
        "ultima_hora": disparo.contar_envios(db, hora_atras),
        "respostas": db.scalar(select(func.count()).select_from(Lead).where(Lead.respondeu_em >= utc(meia_noite))) or 0,
        "reunioes": db.scalar(select(func.count()).select_from(Reuniao).where(Reuniao.created_at >= utc(meia_noite))) or 0,
        "pct_meta": min(round(100 * aberturas_hoje / meta), 100) if meta else 0,
    }

    # Ritmo: quanto da meta já devia ter saído a esta hora do expediente.
    local = como_utc(agora).astimezone(regras.tz)
    minutos_total = disparo.horas_de_disparo(regras) * 60
    minutos_passados = (local.hour * 60 + local.minute) - (regras.inicio.hour * 60 + regras.inicio.minute)
    fracao = min(max(minutos_passados / minutos_total, 0), 1) if minutos_total > 0 else 0
    esperado_agora = round(meta * fracao) if meta and local.weekday() in regras.dias else 0

    numeros = numeros_da_prospeccao(db)
    na_prospeccao = {n.id for n in numeros}
    conectados = [n for n in numeros if n.status == NumeroStatus.CONECTADO]
    cadastrados = numeros_cadastrados(db)
    fora = [n for n in cadastrados if n.id not in na_prospeccao]
    na_hora = disparo.envios_por_numero(db, hora_atras)
    taxas = _taxas_por_numero(db)
    tz = get_settings().timezone
    linhas = []
    # A tabela mostra todos os cadastrados; só os que estão em campanha ativa contam na capacidade.
    for n in sorted(cadastrados, key=lambda n: (n.id not in na_prospeccao, n.id)):
        enviados, respostas = taxas.get(n.id, (0, 0))
        linhas.append(
            {
                "numero": n,
                "em_campanha": n.id in na_prospeccao,
                "dia_rampa": dia_de_rampa(n.data_ativacao, agora, tz) if n.data_ativacao else None,
                "hora": na_hora.get(n.id, 0),
                "enviados": enviados,
                "taxa": round(100 * respostas / enviados, 1) if enviados else None,
            }
        )

    enviados_total = sum(e for e, _ in taxas.values())
    respostas_total = sum(r for _, r in taxas.values())
    taxa_geral = round(100 * respostas_total / enviados_total, 1) if enviados_total else None

    followup_ativo, dias_followup = followup.regras(db)
    # Em regime, cada dia tem follow-up de quem não respondeu, um por passo.
    sem_resposta = 1 - (taxa_geral if taxa_geral is not None else 10) / 100
    carga_followup = round(meta * sem_resposta * len(dias_followup)) if meta and followup_ativo else 0
    teto = volume.teto_por_numero
    necessarios = math.ceil(meta / teto) if meta else 0
    necessarios_com_followup = math.ceil((meta + carga_followup) / teto) if meta else 0

    capacidade_hoje = sum(n.limite_diario_atual for n in conectados)
    na_fila = db.scalar(
        select(func.count())
        .select_from(Lead)
        .join(Campanha, Campanha.id == Lead.campanha_id)
        .where(Lead.status == LeadStatus.NA_FILA, Campanha.status == CampanhaStatus.ATIVA)
    ) or 0
    livres = db.scalar(
        select(func.count()).select_from(Lead).where(Lead.status == LeadStatus.NOVO, Lead.campanha_id.is_(None))
    ) or 0

    capacidade = {
        "conectados": len(conectados),
        "numeros": len(numeros),
        "hoje": capacidade_hoje,
        "restante_hoje": sum(max(n.limite_diario_atual - n.envios_hoje, 0) for n in conectados),
        "por_hora": len(conectados) * volume.limite_hora_numero,
        "necessarios": necessarios,
        "com_reserva": math.ceil(necessarios * RESERVA),
        "faltam": max(math.ceil(necessarios * RESERVA) - len(conectados), 0),
        "necessarios_com_followup": necessarios_com_followup,
        "dias_meta": dia_em_que_alcanca(conectados, rampa, agora, meta) if meta else None,
        "dias_meta_com_followup": dia_em_que_alcanca(conectados, rampa, agora, meta + carga_followup) if meta else None,
    }
    estoque_dias = round(na_fila / meta, 1) if meta else None

    avisos = []
    if fora:
        avisos.append(
            f"{len(fora)} número(s) cadastrado(s) fora das campanhas ativas não entram no disparo. "
            "Use \"Puxar todos os números\" para incluí-los."
        )
    if not meta:
        avisos.append("Defina a meta do dia para o sistema repartir o volume entre os números.")
    else:
        if capacidade["faltam"]:
            avisos.append(
                f"Faltam {capacidade['faltam']} números conectados para a meta "
                f"({capacidade['com_reserva']} com 20% de reserva, a {teto} envios por número)."
            )
        if capacidade_hoje < meta:
            quando = capacidade["dias_meta"]
            avisos.append(
                f"Hoje os números aguentam {capacidade_hoje} envios. "
                + (f"Pela rampa, a meta fecha em {quando} dia(s)." if quando else "Só com mais números a meta fecha.")
            )
        if carga_followup:
            avisos.append(
                f"O follow-up ({len(dias_followup)} passo(s)) deve gastar ~{carga_followup} envios por dia em regime, "
                f"dentro do limite dos números: para a meta de primeiras mensagens, conte com ~{necessarios_com_followup} números."
            )
        if estoque_dias is not None and estoque_dias < ESTOQUE_MINIMO_DIAS:
            avisos.append(
                f"A fila tem {na_fila} leads, menos de {str(ESTOQUE_MINIMO_DIAS).replace('.', ',')} dia de meta. "
                + (f"Há {livres} leads livres para pôr numa campanha. " if livres else "")
                + "Importe uma lista ou faça novas buscas."
            )

    return {
        "meta": meta,
        "meta_hora": disparo.meta_por_hora(regras, meta) if meta else 0,
        "esperado_agora": esperado_agora,
        "no_horario": disparo.dentro_do_horario(regras, agora),
        "volume": volume,
        "hoje": hoje,
        "capacidade": capacidade,
        "carga_followup": carga_followup,
        "followup_passos": len(dias_followup) if followup_ativo else 0,
        "taxa_geral": taxa_geral,
        "na_fila": na_fila,
        "livres": livres,
        "estoque_dias": estoque_dias,
        "linhas": linhas,
        "fora": len(fora),
        "avisos": avisos,
    }
