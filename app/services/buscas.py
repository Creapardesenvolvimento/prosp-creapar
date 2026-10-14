"""Buscas no Google Maps (Apify) → leads com WhatsApp validado.

As funções alteram a sessão mas não fazem commit, exceto `reivindicar_processamento`.
"""

import logging
import time
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import apify, evolution
from app.models import Blacklist, Busca, BuscaStatus, Lead, LeadStatus, NumeroStatus, User, WhatsappNumber
from app.services.telefone import eh_celular, normalizar_telefone, para_evolution

logger = logging.getLogger(__name__)

MOTIVOS_DESCARTE = {
    "fechado": "empresa fechada",
    "sem_telefone": "sem telefone",
    "nao_celular": "telefone fixo",
    "repetido_na_busca": "repetido na própria busca",
    "ja_era_lead": "já era lead",
    "blacklist": "na blacklist",
    "sem_whatsapp": "sem WhatsApp",
}
# Status do run no Apify que já têm resultados para aproveitar (os parciais também).
RUN_COM_RESULTADO = {"SUCCEEDED", "ABORTED", "TIMED-OUT"}
RUN_FALHOU = {"FAILED"}
LOTE_VALIDACAO = 50


class BuscaError(Exception):
    pass


def custo_maximo(limite: int) -> float:
    """Teto de gasto do run: o preço por empresa × limite, com folga. O Apify exige no mínimo US$ 0,50."""
    return max(0.5, round(limite * get_settings().apify_preco_por_lugar * 1.1 + 0.05, 2))


def webhook_url() -> str:
    settings = get_settings()
    return f"{settings.public_base_url.rstrip('/')}/webhooks/apify/{settings.webhook_token}"


def criar_busca(db: Session, termo: str, cidade: str, limite: int, user: User | None = None) -> Busca:
    termo, cidade = termo.strip(), cidade.strip()
    if not termo or not cidade:
        raise BuscaError("Informe o termo e a cidade.")
    maximo = get_settings().busca_limite_maximo
    if not 1 <= limite <= maximo:
        raise BuscaError(f"O limite precisa ficar entre 1 e {maximo}.")
    busca = Busca(termo=termo, cidade=cidade, limite=limite, status=BuscaStatus.PENDENTE, criado_por_id=user.id if user else None)
    db.add(busca)
    db.flush()
    logger.info("busca criada", extra={"busca_id": busca.id, "termo": termo, "cidade": cidade, "limite": limite})
    return busca


def entrada_do_actor(busca: Busca) -> dict:
    return {
        "searchStringsArray": [busca.termo],
        "locationQuery": f"{busca.cidade}, Brasil",
        "maxCrawledPlacesPerSearch": busca.limite,
        "language": "pt-BR",
        "countryCode": "br",
        # Filtros e extras do actor são cobrados à parte; filtramos do nosso lado.
        "skipClosedPlaces": False,
        "scrapeContacts": False,
        "scrapePlaceDetailPage": False,
        "includeWebResults": False,
        "scrapeDirectories": False,
        "maxImages": 0,
        "maxReviews": 0,
        "maxQuestions": 0,
    }


def iniciar(db: Session, busca: Busca) -> None:
    if busca.status != BuscaStatus.PENDENTE:
        return
    try:
        run = apify.get_client().start_run(
            entrada_do_actor(busca), webhook_url=webhook_url(), max_total_charge_usd=custo_maximo(busca.limite)
        )
    except apify.ApifyError as exc:
        busca.status = BuscaStatus.ERRO
        busca.erro = str(exc)
        logger.error("falha ao iniciar busca no Apify", extra={"busca_id": busca.id, "erro": str(exc)})
        return
    busca.apify_run_id = run["id"]
    busca.apify_dataset_id = run.get("defaultDatasetId")
    busca.status = BuscaStatus.EXECUTANDO
    logger.info("busca iniciada no Apify", extra={"busca_id": busca.id, "run_id": run["id"]})


def registrar_fim_do_run(db: Session, busca: Busca, run: dict) -> bool:
    """Aplica o status final do run. Retorna True se há resultados a processar."""
    if busca.status != BuscaStatus.EXECUTANDO:
        return False
    status = run.get("status")
    busca.apify_dataset_id = run.get("defaultDatasetId") or busca.apify_dataset_id
    if status in RUN_COM_RESULTADO:
        logger.info("run do Apify terminou", extra={"busca_id": busca.id, "status": status})
        return True
    if status in RUN_FALHOU:
        busca.status = BuscaStatus.ERRO
        busca.erro = f"O run do Apify terminou com status {status}."
        busca.finished_at = datetime.now(timezone.utc)
        logger.error("run do Apify falhou", extra={"busca_id": busca.id, "status": status})
    return False


def reivindicar_processamento(db: Session, busca_id: int) -> bool:
    """Passa a busca de executando para processando de forma atômica (webhook e polling podem chegar juntos)."""
    result = db.execute(
        update(Busca)
        .where(Busca.id == busca_id, Busca.status == BuscaStatus.EXECUTANDO)
        .values(status=BuscaStatus.PROCESSANDO)
    )
    db.commit()
    return result.rowcount == 1


def _numero_para_validacao(db: Session) -> WhatsappNumber:
    numeros = db.scalars(
        select(WhatsappNumber).where(WhatsappNumber.status.in_([NumeroStatus.CONECTADO, NumeroStatus.PAUSADO]))
    ).all()
    if not numeros:
        raise BuscaError("Nenhum número de WhatsApp conectado para validar os telefones. Conecte um e reprocesse.")
    # Prefere os conectados; entre eles, o que menos enviou hoje.
    return min(numeros, key=lambda n: (n.status != NumeroStatus.CONECTADO, n.envios_hoje))


def validar_whatsapp(db: Session, telefones: list[str], pausa_entre_lotes: float = 1.0) -> dict[str, str | None]:
    """telefone E.164 -> jid (tem WhatsApp) ou None."""
    numero = _numero_para_validacao(db)
    client = evolution.get_client(numero.server)
    resultado: dict[str, str | None] = {}
    for i in range(0, len(telefones), LOTE_VALIDACAO):
        lote = telefones[i : i + LOTE_VALIDACAO]
        if i and pausa_entre_lotes:
            time.sleep(pausa_entre_lotes)
        jids = client.check_whatsapp(numero.instance_name, [para_evolution(t) for t in lote])
        resultado.update({t: jids.get(para_evolution(t)) for t in lote})
    return resultado


def _texto(item: dict, chave: str, tamanho: int) -> str | None:
    valor = item.get(chave)
    return str(valor)[:tamanho] if valor else None


def processar(db: Session, busca: Busca, pausa_entre_lotes: float = 1.0) -> None:
    """Lê o resultado do Apify, filtra, valida WhatsApp e cria os leads. A busca precisa estar em processando."""
    if not busca.apify_dataset_id:
        raise BuscaError("O run do Apify não informou o dataset de resultados.")
    itens = list(apify.get_client().dataset_items(busca.apify_dataset_id))
    descartes: Counter[str] = Counter()
    candidatos: dict[str, dict] = {}
    for item in itens:
        if item.get("permanentlyClosed"):
            descartes["fechado"] += 1
            continue
        telefone = normalizar_telefone(item.get("phoneUnformatted") or item.get("phone"))
        if telefone is None:
            descartes["sem_telefone"] += 1
        elif not eh_celular(telefone):
            descartes["nao_celular"] += 1
        elif telefone in candidatos:
            descartes["repetido_na_busca"] += 1
        else:
            candidatos[telefone] = item

    if candidatos:
        ja_leads = set(db.scalars(select(Lead.telefone).where(Lead.telefone.in_(candidatos))))
        bloqueados = set(db.scalars(select(Blacklist.telefone).where(Blacklist.telefone.in_(candidatos))))
        for telefone in list(candidatos):
            if telefone in bloqueados:
                descartes["blacklist"] += 1
                del candidatos[telefone]
            elif telefone in ja_leads:
                descartes["ja_era_lead"] += 1
                del candidatos[telefone]

    jids = validar_whatsapp(db, list(candidatos), pausa_entre_lotes) if candidatos else {}
    novos = 0
    for telefone, item in candidatos.items():
        if not jids.get(telefone):
            descartes["sem_whatsapp"] += 1
            continue
        lead = Lead(
            busca_id=busca.id,
            nome=(item.get("title") or "Sem nome")[:300],
            telefone=telefone,
            whatsapp_jid=jids[telefone],
            site=_texto(item, "website", 500),
            categoria=_texto(item, "categoryName", 200),
            endereco=_texto(item, "address", 500),
            cidade=_texto(item, "city", 200) or busca.cidade,
            nota=item.get("totalScore"),
            avaliacoes=item.get("reviewsCount"),
            place_id=_texto(item, "placeId", 200),
            status=LeadStatus.NOVO,
        )
        try:
            with db.begin_nested():  # outra busca pode ter criado o mesmo telefone agora há pouco
                db.add(lead)
        except IntegrityError:
            descartes["ja_era_lead"] += 1
            continue
        novos += 1

    busca.total_encontrados = len(itens)
    busca.total_novos = novos
    busca.descartes = dict(descartes)
    busca.status = BuscaStatus.CONCLUIDA
    busca.erro = None
    busca.finished_at = datetime.now(timezone.utc)
    logger.info(
        "busca concluída",
        extra={"busca_id": busca.id, "encontrados": len(itens), "novos": novos, "descartes": dict(descartes)},
    )


def marcar_erro(db: Session, busca: Busca, erro: str) -> None:
    busca.status = BuscaStatus.ERRO
    busca.erro = erro
    busca.finished_at = datetime.now(timezone.utc)
    logger.error("busca com erro", extra={"busca_id": busca.id, "erro": erro})


def preparar_reprocessamento(db: Session, busca: Busca) -> None:
    """Volta uma busca com erro para a fila de processamento (os resultados ficam no Apify por alguns dias)."""
    if busca.status != BuscaStatus.ERRO or not busca.apify_dataset_id:
        raise BuscaError("Só dá para reprocessar uma busca com erro que já tenha resultados no Apify.")
    busca.status = BuscaStatus.EXECUTANDO
    busca.erro = None


def buscas_em_andamento(db: Session) -> list[Busca]:
    return list(
        db.scalars(select(Busca).where(Busca.status == BuscaStatus.EXECUTANDO, Busca.apify_run_id.is_not(None)))
    )
