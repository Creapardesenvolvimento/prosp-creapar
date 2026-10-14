"""Cadastro e ciclo de vida das campanhas.

As funções alteram a sessão mas não fazem commit.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Blacklist,
    Campanha,
    CampanhaMensagem,
    CampanhaStatus,
    Envio,
    EnvioStatus,
    EnvioTipo,
    Lead,
    LeadStatus,
    Reuniao,
    WhatsappNumber,
)
from app.services import ia

logger = logging.getLogger(__name__)

MARCADORES = {"empresa", "cidade"}
_MARCADOR = re.compile(r"\{([^{}]*)\}")


class CampanhaError(Exception):
    pass


def salvar(db: Session, campanha: Campanha | None, *, nome: str, nicho: str, oferta: str, prompt_agente: str) -> Campanha:
    if not nome.strip():
        raise CampanhaError("Informe o nome da campanha.")
    if campanha is None:
        campanha = Campanha(status=CampanhaStatus.RASCUNHO)
        db.add(campanha)
    campanha.nome = nome.strip()
    campanha.nicho = nicho.strip()
    campanha.oferta = oferta.strip()
    campanha.prompt_agente = prompt_agente.strip()
    db.flush()
    return campanha


def validar_texto(texto: str) -> str:
    texto = texto.strip()
    if not texto:
        raise CampanhaError("A mensagem está vazia.")
    if len(texto) > 1000:
        raise CampanhaError("A mensagem passou de 1000 caracteres.")
    desconhecidos = {m for m in _MARCADOR.findall(texto)} - MARCADORES
    if desconhecidos:
        raise CampanhaError(f"Marcador desconhecido: {{{sorted(desconhecidos)[0]}}}. Use só {{empresa}} e {{cidade}}.")
    return texto


def adicionar_mensagem(db: Session, campanha: Campanha, texto: str, origem: str = "manual") -> CampanhaMensagem:
    mensagem = CampanhaMensagem(campanha=campanha, texto=validar_texto(texto), origem=origem, ativa=True)
    db.add(mensagem)
    db.flush()
    return mensagem


def remover_mensagem(db: Session, mensagem: CampanhaMensagem) -> None:
    usada = db.scalar(select(func.count()).select_from(Envio).where(Envio.mensagem_id == mensagem.id))
    if usada:
        mensagem.ativa = False  # mantém o histórico dos envios que usaram esta variação
    else:
        db.delete(mensagem)
    db.flush()


def gerar_mensagens_ia(db: Session, campanha: Campanha, quantidade: int = 10) -> int:
    """Pede variações ao Claude e guarda as válidas. Retorna quantas entraram."""
    existentes = [m.texto for m in campanha.mensagens if m.ativa]
    novas = 0
    for texto in ia.gerar_variacoes(nicho=campanha.nicho, oferta=campanha.oferta, exemplos=existentes, quantidade=quantidade):
        try:
            adicionar_mensagem(db, campanha, texto, origem="ia")
        except CampanhaError:
            continue  # a IA às vezes inventa marcador; descarta essa variação
        novas += 1
    logger.info("variações da IA adicionadas", extra={"campanha_id": campanha.id, "novas": novas})
    return novas


def renderizar(texto: str, lead: Lead) -> str:
    cidade = (lead.cidade or "").strip() or "sua cidade"
    texto = texto.replace("{empresa}", lead.nome.strip()).replace("{cidade}", cidade)
    return re.sub(r"[ \t]{2,}", " ", texto).strip()


def definir_numeros(db: Session, campanha: Campanha, numero_ids: list[int]) -> None:
    campanha.numeros = list(db.scalars(select(WhatsappNumber).where(WhatsappNumber.id.in_(numero_ids)))) if numero_ids else []
    db.flush()


def adicionar_leads(db: Session, campanha: Campanha, busca_ids: list[int]) -> int:
    """Põe na fila da campanha os leads novos (ainda sem campanha) das buscas escolhidas."""
    if campanha.status == CampanhaStatus.ENCERRADA:
        raise CampanhaError("A campanha está encerrada.")
    if not busca_ids:
        raise CampanhaError("Escolha pelo menos uma busca.")
    bloqueados = select(Blacklist.telefone)
    result = db.execute(
        update(Lead)
        .where(
            Lead.busca_id.in_(busca_ids),
            Lead.status == LeadStatus.NOVO,
            Lead.campanha_id.is_(None),
            Lead.telefone.not_in(bloqueados),
        )
        .values(campanha_id=campanha.id, status=LeadStatus.NA_FILA)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    logger.info("leads adicionados à campanha", extra={"campanha_id": campanha.id, "total": result.rowcount})
    return result.rowcount


def ativar(db: Session, campanha: Campanha) -> None:
    if campanha.status not in (CampanhaStatus.RASCUNHO, CampanhaStatus.PAUSADA):
        raise CampanhaError("Só dá para ativar uma campanha em rascunho ou pausada.")
    if not any(m.ativa for m in campanha.mensagens):
        raise CampanhaError("Cadastre pelo menos uma mensagem de abertura.")
    if not campanha.numeros:
        raise CampanhaError("Escolha pelo menos um número para disparar.")
    campanha.status = CampanhaStatus.ATIVA
    logger.info("campanha ativada", extra={"campanha_id": campanha.id})


def pausar(db: Session, campanha: Campanha) -> None:
    if campanha.status != CampanhaStatus.ATIVA:
        raise CampanhaError("A campanha não está ativa.")
    campanha.status = CampanhaStatus.PAUSADA
    logger.info("campanha pausada", extra={"campanha_id": campanha.id})


def encerrar(db: Session, campanha: Campanha) -> int:
    """Encerra e devolve os leads que ainda não receberam mensagem (podem ir para outra campanha)."""
    if campanha.status == CampanhaStatus.ENCERRADA:
        raise CampanhaError("A campanha já está encerrada.")
    campanha.status = CampanhaStatus.ENCERRADA
    result = db.execute(
        update(Lead)
        .where(Lead.campanha_id == campanha.id, Lead.status == LeadStatus.NA_FILA, Lead.numero_id.is_(None))
        .values(campanha_id=None, status=LeadStatus.NOVO)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    logger.info("campanha encerrada", extra={"campanha_id": campanha.id, "leads_devolvidos": result.rowcount})
    return result.rowcount


def metricas(db: Session, campanha: Campanha) -> dict:
    def contar(*filtros) -> int:
        return db.scalar(select(func.count()).select_from(Lead).where(Lead.campanha_id == campanha.id, *filtros)) or 0

    inicio_dia = datetime.now(timezone.utc) - timedelta(hours=24)
    enviados = contar(Lead.enviado_em.is_not(None))
    respostas = contar(Lead.respondeu_em.is_not(None))
    lead_ids = select(Lead.id).where(Lead.campanha_id == campanha.id)
    return {
        "na_fila": contar(Lead.status == LeadStatus.NA_FILA, Lead.enviado_em.is_(None)),
        "enviados": enviados,
        "enviados_24h": db.scalar(
            select(func.count())
            .select_from(Envio)
            .where(
                Envio.campanha_id == campanha.id,
                Envio.tipo == EnvioTipo.ABERTURA,
                Envio.status == EnvioStatus.ENVIADO,
                Envio.enviado_em >= inicio_dia,
            )
        )
        or 0,
        "respostas": respostas,
        "taxa_resposta": round(100 * respostas / enviados, 1) if enviados else 0.0,
        "opt_outs": contar(Lead.status == LeadStatus.OPT_OUT),
        "reunioes": db.scalar(select(func.count()).select_from(Reuniao).where(Reuniao.lead_id.in_(lead_ids))) or 0,
        "falhas": db.scalar(
            select(func.count()).select_from(Envio).where(Envio.campanha_id == campanha.id, Envio.status == EnvioStatus.FALHOU)
        )
        or 0,
    }
