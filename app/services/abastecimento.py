"""Abastecimento automático: quando a fila de leads baixa, dispara a próxima busca do plano no Apify.

O plano é uma lista de termo × cidade, cada um com a campanha que recebe os leads. Uma busca por vez,
com teto de gasto por dia; a mesma combinação não é buscada de novo antes de 90 dias.

As funções alteram a sessão mas não fazem commit.
"""

import logging
import math
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Busca, BuscaStatus, Campanha, CampanhaStatus, Lead, LeadStatus, NumeroStatus, PlanoBusca
from app.services import buscas, config_sistema
from app.services.agenda import utc

logger = logging.getLogger(__name__)

INTERVALO_REPETICAO = timedelta(days=90)  # igual ao aviso de busca repetida da tela de buscas
EM_CURSO = (BuscaStatus.PENDENTE, BuscaStatus.EXECUTANDO, BuscaStatus.PROCESSANDO)


class AbastecimentoError(Exception):
    pass


def _cidades(texto: str) -> list[str]:
    vistas, cidades = set(), []
    for parte in re.split(r"[\n;]", texto or ""):
        cidade = parte.strip()
        if cidade and cidade.lower() not in vistas:
            vistas.add(cidade.lower())
            cidades.append(cidade[:200])
    return cidades


def adicionar_ao_plano(db: Session, *, termo: str, cidades: str, limite: int, campanha: Campanha | None) -> int:
    """Um item por cidade (uma por linha). Retorna quantos entraram; os repetidos são ignorados."""
    termo = termo.strip()[:200]
    if not termo:
        raise AbastecimentoError("Informe o que buscar.")
    lista = _cidades(cidades)
    if not lista:
        raise AbastecimentoError("Informe pelo menos uma cidade (uma por linha).")
    maximo = get_settings().busca_limite_maximo
    if not 1 <= limite <= maximo:
        raise AbastecimentoError(f"O máximo de empresas por busca precisa ficar entre 1 e {maximo}.")
    if campanha is None or campanha.status == CampanhaStatus.ENCERRADA:
        # Sem campanha os leads ficariam livres, a fila não subiria e o abastecimento buscaria sem parar.
        raise AbastecimentoError("Escolha a campanha que recebe os leads.")
    existentes = {
        (t.lower(), c.lower())
        for t, c in db.execute(select(PlanoBusca.termo, PlanoBusca.cidade).where(PlanoBusca.ativo))
    }
    novos = 0
    for cidade in lista:
        if (termo.lower(), cidade.lower()) in existentes:
            continue
        db.add(PlanoBusca(termo=termo, cidade=cidade, limite=limite, campanha_id=campanha.id, ativo=True))
        novos += 1
    db.flush()
    logger.info("plano de buscas ampliado", extra={"termo": termo, "novos": novos, "campanha_id": campanha.id})
    return novos


def remover_do_plano(db: Session, item: PlanoBusca) -> None:
    if item.busca_id:
        item.ativo = False  # guarda o histórico de que essa combinação já foi buscada
    else:
        db.delete(item)
    db.flush()


def inicio_do_dia(agora: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    local = agora.astimezone(ZoneInfo(get_settings().timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def gasto_hoje(db: Session, agora: datetime) -> float:
    """Soma dos tetos de gasto das buscas no Apify criadas hoje (o custo real fica igual ou abaixo)."""
    limites = db.scalars(
        select(Busca.limite).where(Busca.origem == "apify", Busca.created_at >= utc(inicio_do_dia(agora)))
    ).all()
    return round(sum(buscas.custo_maximo(limite) for limite in limites), 2)


def alvo_de_estoque(db: Session) -> int:
    """Quantos leads na fila manter: dias de estoque × meta do dia (ou, sem meta, × o que os números aguentam)."""
    from app.services.prospeccao import numeros_da_prospeccao

    v = config_sistema.valores(db)
    base = int(v["meta_diaria"]) or sum(
        n.limite_diario_atual for n in numeros_da_prospeccao(db) if n.status == NumeroStatus.CONECTADO
    )
    return math.ceil(base * float(v["abastecer_estoque_dias"]))


def na_fila(db: Session) -> int:
    """Leads na fila das campanhas que o plano abastece."""
    do_plano = select(PlanoBusca.campanha_id).where(PlanoBusca.ativo, PlanoBusca.campanha_id.is_not(None))
    return db.scalar(
        select(func.count())
        .select_from(Lead)
        .join(Campanha, Campanha.id == Lead.campanha_id)
        .where(Lead.status == LeadStatus.NA_FILA, Campanha.status != CampanhaStatus.ENCERRADA, Campanha.id.in_(do_plano))
    ) or 0


def proximo_item(db: Session, agora: datetime) -> PlanoBusca | None:
    """O próximo do plano que ainda pode ser buscado: nunca buscado (ou há mais de 90 dias), com campanha em uso."""
    candidatos = db.scalars(
        select(PlanoBusca)
        .join(Campanha, Campanha.id == PlanoBusca.campanha_id)
        .where(
            PlanoBusca.ativo,
            Campanha.status != CampanhaStatus.ENCERRADA,
            (PlanoBusca.buscado_em.is_(None)) | (PlanoBusca.buscado_em < utc(agora - INTERVALO_REPETICAO)),
        )
        .order_by(PlanoBusca.buscado_em.is_not(None), PlanoBusca.buscado_em, PlanoBusca.id)
    ).all()
    for item in candidatos:
        parecida = next(iter(buscas.buscas_parecidas(db, item.termo, item.cidade)), None)
        if parecida is None:
            return item
        # Alguém já buscou isso na mão há pouco: conta como buscado e segue para o próximo.
        item.busca_id, item.buscado_em = parecida.id, utc(parecida.created_at or agora)
    return None


def situacao(db: Session, agora: datetime | None = None) -> dict:
    agora = utc(agora or datetime.now(timezone.utc))
    v = config_sistema.valores(db)
    em_curso = db.scalar(
        select(Busca).where(Busca.origem == "apify", Busca.status.in_(EM_CURSO)).order_by(Busca.id.desc()).limit(1)
    )
    return {
        "ativo": bool(v["abastecer_ativo"]),
        "teto": float(v["abastecer_gasto_diario"]),
        "estoque_dias": float(v["abastecer_estoque_dias"]),
        "gasto_hoje": gasto_hoje(db, agora),
        "alvo": alvo_de_estoque(db),
        "na_fila": na_fila(db),
        "em_curso": em_curso,
        "itens": db.scalars(
            select(PlanoBusca).where(PlanoBusca.ativo).order_by(PlanoBusca.buscado_em.is_not(None), PlanoBusca.buscado_em, PlanoBusca.id)
        ).all(),
    }


def abastecer(db: Session, agora: datetime | None = None) -> Busca | None:
    """Cria a próxima busca do plano se a fila estiver abaixo do alvo. Quem chama inicia a busca depois do commit."""
    agora = utc(agora or datetime.now(timezone.utc))
    v = config_sistema.valores(db)
    if not v["abastecer_ativo"] or not get_settings().apify_token:
        return None  # sem token toda busca daria erro e o plano seria gasto à toa
    if db.scalar(select(Busca.id).where(Busca.origem == "apify", Busca.status.in_(EM_CURSO)).limit(1)):
        return None  # uma por vez: os leads da busca em curso ainda vão entrar na fila
    alvo, atual = alvo_de_estoque(db), na_fila(db)
    if atual >= alvo:
        return None
    item = proximo_item(db, agora)
    if item is None:
        logger.warning("abastecimento: o plano de buscas acabou", extra={"na_fila": atual, "alvo": alvo})
        return None
    gasto, custo, teto = gasto_hoje(db, agora), buscas.custo_maximo(item.limite), float(v["abastecer_gasto_diario"])
    if gasto + custo > teto:
        logger.info("abastecimento: teto de gasto do dia atingido", extra={"gasto": gasto, "teto": teto})
        return None
    busca = buscas.criar_busca(db, item.termo, item.cidade, item.limite)
    busca.campanha_id = item.campanha_id
    item.busca_id, item.buscado_em = busca.id, agora
    logger.info(
        "abastecimento: busca criada",
        extra={"busca_id": busca.id, "termo": item.termo, "cidade": item.cidade, "na_fila": atual, "alvo": alvo},
    )
    return busca
