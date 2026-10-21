"""Cadastro e ciclo de vida das campanhas.

As funções alteram a sessão mas não fazem commit.
"""

import logging
import random
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Blacklist,
    Campanha,
    CampanhaFollowup,
    CampanhaMensagem,
    CampanhaModo,
    CampanhaStatus,
    Envio,
    EnvioStatus,
    EnvioTipo,
    Lead,
    LeadStatus,
    NumeroStatus,
    Reuniao,
    WhatsappNumber,
)
from app.services import config_sistema, historico, ia
from app.services.agenda import como_utc

logger = logging.getLogger(__name__)

MARCADORES = {"empresa", "cidade"}
_MARCADOR = re.compile(r"\{([^{}]*)\}")


class CampanhaError(Exception):
    pass


def salvar(
    db: Session, campanha: Campanha | None, *, nome: str, nicho: str, oferta: str, prompt_agente: str, modo: str | None = None
) -> Campanha:
    if not nome.strip():
        raise CampanhaError("Informe o nome da campanha.")
    if modo is not None and modo not in (CampanhaModo.NORMAL, CampanhaModo.SUPER):
        raise CampanhaError("Tipo de campanha inválido.")
    if campanha is None:
        campanha = Campanha(status=CampanhaStatus.RASCUNHO)
        db.add(campanha)
    campanha.nome = nome.strip()
    campanha.nicho = nicho.strip()
    campanha.oferta = oferta.strip()
    campanha.prompt_agente = prompt_agente.strip()
    if modo is not None:
        campanha.modo = modo
    db.flush()
    return campanha


def validar_texto(texto: str) -> str:
    texto = texto.strip()
    if not texto:
        raise CampanhaError("A mensagem está vazia.")
    if len(texto) > 1000:
        raise CampanhaError("A mensagem passou de 1000 caracteres.")
    # {a|b|c} é variação sorteada a cada envio; sem "|", só valem os marcadores conhecidos.
    desconhecidos = {m for m in _MARCADOR.findall(texto) if "|" not in m} - MARCADORES
    if desconhecidos:
        raise CampanhaError(
            f"Marcador desconhecido: {{{sorted(desconhecidos)[0]}}}. Use {{empresa}}, {{cidade}} ou variações como {{Oi|Olá}}."
        )
    return texto


def adicionar_mensagem(db: Session, campanha: Campanha, texto: str, origem: str = "manual") -> CampanhaMensagem:
    mensagem = CampanhaMensagem(campanha=campanha, texto=validar_texto(texto), origem=origem, ativa=True)
    db.add(mensagem)
    db.flush()
    return mensagem


def adicionar_followup(db: Session, campanha: Campanha, passo: int, texto: str) -> CampanhaFollowup:
    if passo < 1 or passo > 5:
        raise CampanhaError("Passo do follow-up inválido.")
    followup = CampanhaFollowup(campanha_id=campanha.id, passo=passo, texto=validar_texto(texto))
    db.add(followup)
    db.flush()
    db.refresh(campanha)
    return followup


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
    for texto in ia.gerar_variacoes(
        nicho=campanha.nicho, oferta=campanha.oferta, exemplos=existentes, quantidade=quantidade, db=db
    ):
        try:
            adicionar_mensagem(db, campanha, texto, origem="ia")
        except CampanhaError:
            continue  # a IA às vezes inventa marcador; descarta essa variação
        novas += 1
    logger.info("variações da IA adicionadas", extra={"campanha_id": campanha.id, "novas": novas})
    return novas


def sortear_variacoes(texto: str, rng: random.Random | None = None) -> str:
    """ "{Oi|Olá}, tudo bem?" -> "Olá, tudo bem?" (uma alternativa sorteada para cada trecho)."""
    rng = rng or random.Random()
    return _MARCADOR.sub(lambda m: rng.choice(m.group(1).split("|")) if "|" in m.group(1) else m.group(0), texto)


def contar_combinacoes(texto: str) -> int:
    """Quantos textos diferentes uma mensagem com variações gera."""
    total = 1
    for marcador in _MARCADOR.findall(texto):
        if "|" in marcador:
            total *= len(set(marcador.split("|")))
    return total


def renderizar(texto: str, lead: Lead, rng: random.Random | None = None) -> str:
    cidade = (lead.cidade or "").strip() or "sua cidade"
    empresa = lead.nome.strip() or "sua empresa"  # listas importadas podem vir só com o telefone
    texto = sortear_variacoes(texto, rng).replace("{empresa}", empresa).replace("{cidade}", cidade)
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
    livres = (Lead.busca_id.in_(busca_ids), Lead.status == LeadStatus.NOVO, Lead.campanha_id.is_(None))
    # Quem já foi abordado (ou tem o mesmo WhatsApp de quem foi) sai da lista de disponíveis de vez.
    ja = db.execute(
        update(Lead)
        .where(*livres, historico.condicao_ja_prospectado())
        .values(status=LeadStatus.JA_PROSPECTADO)
        .execution_options(synchronize_session=False)
    )
    result = db.execute(
        update(Lead)
        .where(*livres, Lead.telefone.not_in(bloqueados))
        .values(campanha_id=campanha.id, status=LeadStatus.NA_FILA)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    logger.info(
        "leads adicionados à campanha",
        extra={"campanha_id": campanha.id, "total": result.rowcount, "ja_prospectados": ja.rowcount},
    )
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


# Fases do lead na barra de progresso, na ordem em que aparecem.
FASES_PROGRESSO = [
    ("reuniao", "Reunião agendada", [LeadStatus.REUNIAO_AGENDADA]),
    ("conversa", "Em conversa", [LeadStatus.RESPONDEU, LeadStatus.EM_CONVERSA]),
    ("humano", "Com humano", [LeadStatus.HUMANO_ASSUMIU]),
    ("aguardando", "Aguardando resposta", [LeadStatus.ENVIADO]),
    ("encerrado", "Encerrados", [LeadStatus.SEM_INTERESSE, LeadStatus.OPT_OUT, LeadStatus.SEM_RESPOSTA, LeadStatus.JA_PROSPECTADO]),
    ("fila", "Na fila", [LeadStatus.NA_FILA]),
]


def progresso(db: Session, campanha: Campanha) -> dict:
    """Quanto da fila da campanha já foi contatado e em que pé estão os leads."""
    contagem = dict(
        db.execute(
            select(Lead.status, func.count()).where(Lead.campanha_id == campanha.id).group_by(Lead.status)
        ).all()
    )
    total = sum(contagem.values())
    fases = []
    for chave, rotulo, status in FASES_PROGRESSO:
        quantidade = sum(contagem.get(s, 0) for s in status)
        fases.append({"chave": chave, "rotulo": rotulo, "quantidade": quantidade, "pct": round(100 * quantidade / total, 1) if total else 0})
    na_fila = next(f["quantidade"] for f in fases if f["chave"] == "fila")
    contatados = total - na_fila

    # Capacidade: o que os números da campanha ainda podem mandar hoje e por dia (limite da rampa).
    conectados = [n for n in campanha.numeros if n.status == NumeroStatus.CONECTADO]
    por_dia = sum(n.limite_diario_atual for n in conectados)
    restante_hoje = sum(max(n.limite_diario_atual - n.envios_hoje, 0) for n in conectados)
    if na_fila == 0 or por_dia == 0:
        dias = None
    elif na_fila <= restante_hoje:
        dias = 0  # termina hoje
    else:
        dias = -(-(na_fila - restante_hoje) // por_dia)  # arredonda para cima
    return {
        "total": total,
        "contatados": contatados,
        "na_fila": na_fila,
        "pct_contatados": round(100 * contatados / total) if total else 0,
        "fases": fases,
        "por_dia": por_dia,
        "restante_hoje": restante_hoje,
        "dias_restantes": dias,
        "sem_numero": not conectados,
        "concluida": total > 0 and na_fila == 0,
        "vazia": total == 0,
        "diagnostico": diagnostico(db, campanha) if campanha.status == CampanhaStatus.ATIVA and na_fila else None,
    }


DIAS_CURTOS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def diagnostico(db: Session, campanha: Campanha, agora: datetime | None = None) -> dict:
    """Explica por que a fila está (ou não) andando agora: horário e situação de cada número."""
    from app.services.disparo import dentro_do_horario

    agora = agora or datetime.now(timezone.utc)
    regras = config_sistema.regras_disparo(db)
    dias = ", ".join(DIAS_CURTOS[d] for d in sorted(regras.dias))
    horario = f"{dias}, {regras.inicio:%H:%M}–{regras.fim:%H:%M}"
    numeros = []
    for n in sorted(campanha.numeros, key=lambda n: n.id):
        if n.status != NumeroStatus.CONECTADO:
            motivo = n.motivo_pausa if n.status == NumeroStatus.PAUSADO and n.motivo_pausa else ""
            texto, ok = f"{n.status.replace('_', ' ')}{f' ({motivo})' if motivo else ''}: não envia", False
        elif n.envios_hoje >= n.limite_diario_atual:
            texto, ok = f"limite de hoje atingido ({n.envios_hoje}/{n.limite_diario_atual}): volta amanhã", False
        elif n.proximo_envio_em and como_utc(n.proximo_envio_em) > agora:
            proximo = como_utc(n.proximo_envio_em).astimezone(regras.tz)
            texto, ok = f"{n.envios_hoje}/{n.limite_diario_atual} hoje · próximo envio às {proximo:%H:%M}", True
        else:
            texto, ok = f"{n.envios_hoje}/{n.limite_diario_atual} hoje · envia no próximo ciclo", True
        numeros.append({"nome": n.nome_exibicao, "texto": texto, "ok": ok})
    return {"no_horario": dentro_do_horario(regras, agora), "horario": horario, "numeros": numeros}


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
        "followups": db.scalar(
            select(func.count())
            .select_from(Envio)
            .where(Envio.campanha_id == campanha.id, Envio.tipo == EnvioTipo.FOLLOW_UP, Envio.status == EnvioStatus.ENVIADO)
        )
        or 0,
        "sem_resposta": contar(Lead.status == LeadStatus.SEM_RESPOSTA),
        "respostas": respostas,
        "taxa_resposta": round(100 * respostas / enviados, 1) if enviados else 0.0,
        "opt_outs": contar(Lead.status == LeadStatus.OPT_OUT),
        "reunioes": db.scalar(select(func.count()).select_from(Reuniao).where(Reuniao.lead_id.in_(lead_ids))) or 0,
        "falhas": db.scalar(
            select(func.count()).select_from(Envio).where(Envio.campanha_id == campanha.id, Envio.status == EnvioStatus.FALHOU)
        )
        or 0,
    }
