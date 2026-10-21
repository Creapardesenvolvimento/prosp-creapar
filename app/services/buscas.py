"""Buscas no Google Maps (Apify) → leads com WhatsApp validado.

As funções alteram a sessão mas não fazem commit, exceto `reivindicar_processamento`.
"""

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import apify, evolution
from app.models import Blacklist, Busca, BuscaStatus, Empresa, Lead, LeadStatus, NumeroStatus, User, WhatsappNumber
from app.services.telefone import eh_celular, normalizar_telefone, para_evolution

logger = logging.getLogger(__name__)

MOTIVOS_DESCARTE = {
    "fechado": "empresa fechada",
    "sem_telefone": "sem telefone",
    "telefone_invalido": "telefone inválido",
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
LOTE_CONSULTA = 1000  # telefones por consulta ao banco (listas importadas podem ter milhares)


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


def _numeros_para_validacao(db: Session) -> list[WhatsappNumber]:
    numeros = db.scalars(
        select(WhatsappNumber).where(WhatsappNumber.status.in_([NumeroStatus.CONECTADO, NumeroStatus.PAUSADO]))
    ).all()
    if not numeros:
        raise BuscaError("Nenhum número de WhatsApp conectado para validar os telefones. Conecte um e reprocesse.")
    conectados = [n for n in numeros if n.status == NumeroStatus.CONECTADO]
    # Prefere os conectados, os que menos enviaram hoje primeiro.
    return sorted(conectados or numeros, key=lambda n: (n.envios_hoje, n.id))


def validar_whatsapp(db: Session, telefones: list[str], pausa_entre_lotes: float = 1.0) -> dict[str, str | None]:
    """telefone E.164 -> jid (tem WhatsApp) ou None. Os lotes se revezam entre os números conectados,
    para que uma lista grande não concentre milhares de consultas num chip só."""
    numeros = _numeros_para_validacao(db)
    resultado: dict[str, str | None] = {}
    for indice, i in enumerate(range(0, len(telefones), LOTE_VALIDACAO)):
        lote = telefones[i : i + LOTE_VALIDACAO]
        if i and pausa_entre_lotes:
            time.sleep(pausa_entre_lotes)
        numero = numeros[indice % len(numeros)]
        jids = evolution.get_client(numero.server).check_whatsapp(numero.instance_name, [para_evolution(t) for t in lote])
        resultado.update({t: jids.get(para_evolution(t)) for t in lote})
    return resultado


def _em_lotes(itens: list[str]):
    for i in range(0, len(itens), LOTE_CONSULTA):
        yield itens[i : i + LOTE_CONSULTA]


def _texto(item: dict, chave: str, tamanho: int) -> str | None:
    valor = item.get(chave)
    return str(valor)[:tamanho] if valor else None


def _empresa_do_item(busca: Busca, item: dict) -> Empresa:
    original = item.get("phoneUnformatted") or item.get("phone")
    telefone = normalizar_telefone(original)
    return Empresa(
        busca_id=busca.id,
        place_id=_texto(item, "placeId", 200),
        nome=(item.get("title") or "Sem nome")[:300],
        telefone_original=str(original)[:50] if original else None,
        telefone=telefone,
        celular=eh_celular(telefone),
        site=_texto(item, "website", 500),
        categoria=_texto(item, "categoryName", 200),
        endereco=_texto(item, "address", 500),
        bairro=_texto(item, "neighborhood", 200),
        cidade=_texto(item, "city", 200),
        estado=_texto(item, "state", 100),
        nota=item.get("totalScore"),
        avaliacoes=item.get("reviewsCount"),
        url_maps=_texto(item, "url", 1000),
        dados=item,
    )


def guardar_resultados(db: Session, busca: Busca) -> int:
    """Copia para o banco todas as empresas que o Apify devolveu (o dataset de lá expira em poucos dias).

    Se já estiverem guardadas, não baixa de novo. Retorna quantas empresas a busca tem.
    """
    existentes = db.scalar(select(func.count()).select_from(Empresa).where(Empresa.busca_id == busca.id))
    if existentes:
        return existentes
    if not busca.apify_dataset_id:
        raise BuscaError("O run do Apify não informou o dataset de resultados.")
    vistos: set[str] = set()
    total = 0
    for item in apify.get_client().dataset_items(busca.apify_dataset_id):
        empresa = _empresa_do_item(busca, item)
        if empresa.place_id:
            if empresa.place_id in vistos:
                continue
            vistos.add(empresa.place_id)
        db.add(empresa)
        total += 1
    db.flush()
    logger.info("resultados do Apify guardados", extra={"busca_id": busca.id, "empresas": total})
    return total


def processar(db: Session, busca: Busca, pausa_entre_lotes: float = 1.0) -> None:
    """Filtra as empresas guardadas, valida WhatsApp e cria os leads. A busca precisa estar em processando."""
    guardar_resultados(db, busca)
    empresas = db.scalars(select(Empresa).where(Empresa.busca_id == busca.id).order_by(Empresa.id)).all()
    descartes: Counter[str] = Counter()
    candidatos: dict[str, Empresa] = {}

    def descartar(empresa: Empresa, motivo: str) -> None:
        empresa.descarte = motivo
        descartes[motivo] += 1

    for empresa in empresas:
        empresa.descarte, empresa.lead_id = None, None  # reprocessamento recomeça do zero
        if empresa.dados.get("permanentlyClosed"):
            descartar(empresa, "fechado")
        elif empresa.telefone is None:
            descartar(empresa, "telefone_invalido" if busca.importada and empresa.telefone_original else "sem_telefone")
        elif not empresa.celular and not busca.importada:
            # No Google Maps o fixo quase nunca é WhatsApp. Numa lista importada pode ser WhatsApp Business: confere.
            descartar(empresa, "nao_celular")
        elif empresa.telefone in candidatos:
            descartar(empresa, "repetido_na_busca")
        else:
            candidatos[empresa.telefone] = empresa

    if candidatos:
        ja_leads: dict[str, int] = {}
        bloqueados: set[str] = set()
        for lote in _em_lotes(list(candidatos)):
            ja_leads.update(db.execute(select(Lead.telefone, Lead.id).where(Lead.telefone.in_(lote))).all())
            bloqueados.update(db.scalars(select(Blacklist.telefone).where(Blacklist.telefone.in_(lote))))
        for telefone, empresa in list(candidatos.items()):
            if telefone in bloqueados:
                descartar(empresa, "blacklist")
                del candidatos[telefone]
            elif telefone in ja_leads:
                descartar(empresa, "ja_era_lead")
                empresa.lead_id = ja_leads[telefone]
                del candidatos[telefone]

    jids = validar_whatsapp(db, list(candidatos), pausa_entre_lotes) if candidatos else {}
    novos = 0
    for telefone, empresa in candidatos.items():
        if not jids.get(telefone):
            descartar(empresa, "sem_whatsapp")
            continue
        lead = Lead(
            busca_id=busca.id,
            nome=empresa.nome,
            telefone=telefone,
            whatsapp_jid=jids[telefone],
            site=empresa.site,
            categoria=empresa.categoria,
            endereco=empresa.endereco,
            cidade=empresa.cidade or busca.cidade,
            nota=empresa.nota,
            avaliacoes=empresa.avaliacoes,
            place_id=empresa.place_id,
            status=LeadStatus.NOVO,
        )
        try:
            with db.begin_nested():  # outra busca pode ter criado o mesmo telefone agora há pouco
                db.add(lead)
        except IntegrityError:
            descartar(empresa, "ja_era_lead")
            continue
        empresa.lead_id = lead.id
        novos += 1

    busca.total_encontrados = len(empresas)
    busca.total_novos = novos
    busca.descartes = dict(descartes)
    busca.status = BuscaStatus.CONCLUIDA
    busca.erro = None
    busca.finished_at = datetime.now(timezone.utc)
    if busca.campanha_id and novos:
        _mandar_para_campanha(db, busca)
    logger.info(
        "busca concluída",
        extra={"busca_id": busca.id, "encontrados": len(empresas), "novos": novos, "descartes": dict(descartes)},
    )


def _mandar_para_campanha(db: Session, busca: Busca) -> None:
    """Lista importada com campanha escolhida: os leads novos entram direto na fila dela."""
    from app.models import Campanha
    from app.services import campanhas

    campanha = db.get(Campanha, busca.campanha_id)
    if campanha is None:
        return
    try:
        campanhas.adicionar_leads(db, campanha, [busca.id])
    except campanhas.CampanhaError as exc:  # encerrada depois da importação: os leads ficam livres
        logger.warning("lista não entrou na campanha", extra={"busca_id": busca.id, "erro": str(exc)})


def recuperar_empresas(db: Session, busca: Busca) -> int:
    """Para buscas feitas antes de as empresas serem guardadas: baixa do Apify (ler não custa nada)
    e marca cada empresa sem revalidar WhatsApp nem criar leads de novo."""
    if busca.status != BuscaStatus.CONCLUIDA:
        raise BuscaError("Só dá para recuperar uma busca concluída.")
    if db.scalar(select(func.count()).select_from(Empresa).where(Empresa.busca_id == busca.id)):
        raise BuscaError("As empresas desta busca já estão guardadas.")
    try:
        total = guardar_resultados(db, busca)
    except apify.ApifyError as exc:
        raise BuscaError(f"O Apify não devolveu mais os resultados (eles expiram depois de alguns dias): {exc}") from exc
    leads_da_busca = dict(db.execute(select(Lead.telefone, Lead.id).where(Lead.busca_id == busca.id)).all())
    outros_leads = dict(db.execute(select(Lead.telefone, Lead.id).where(Lead.busca_id != busca.id)).all())
    bloqueados = set(db.scalars(select(Blacklist.telefone)))
    vistos: set[str] = set()
    for empresa in db.scalars(select(Empresa).where(Empresa.busca_id == busca.id).order_by(Empresa.id)):
        if empresa.telefone in leads_da_busca and empresa.telefone not in vistos:
            empresa.lead_id = leads_da_busca[empresa.telefone]
        elif empresa.dados.get("permanentlyClosed"):
            empresa.descarte = "fechado"
        elif empresa.telefone is None:
            empresa.descarte = "sem_telefone"
        elif not empresa.celular:
            empresa.descarte = "nao_celular"
        elif empresa.telefone in vistos:
            empresa.descarte = "repetido_na_busca"
        elif empresa.telefone in bloqueados:
            empresa.descarte = "blacklist"
        elif empresa.telefone in outros_leads:
            empresa.descarte, empresa.lead_id = "ja_era_lead", outros_leads[empresa.telefone]
        else:
            empresa.descarte = "sem_whatsapp"  # era celular e não virou lead
        if empresa.telefone:
            vistos.add(empresa.telefone)
    db.flush()
    logger.info("empresas de busca antiga recuperadas", extra={"busca_id": busca.id, "empresas": total})
    return total


def buscas_parecidas(db: Session, termo: str, cidade: str, dias: int = 90) -> list[Busca]:
    """Buscas recentes com o mesmo termo e cidade, para não pagar duas vezes pela mesma lista."""
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    return list(
        db.scalars(
            select(Busca)
            .where(
                func.lower(Busca.termo) == termo.strip().lower(),
                func.lower(Busca.cidade) == cidade.strip().lower(),
                Busca.status.in_([BuscaStatus.CONCLUIDA, BuscaStatus.EXECUTANDO, BuscaStatus.PROCESSANDO]),
                Busca.created_at >= desde,
            )
            .order_by(Busca.created_at.desc())
        )
    )


def marcar_erro(db: Session, busca: Busca, erro: str) -> None:
    busca.status = BuscaStatus.ERRO
    busca.erro = erro
    busca.finished_at = datetime.now(timezone.utc)
    logger.error("busca com erro", extra={"busca_id": busca.id, "erro": erro})


def preparar_reprocessamento(db: Session, busca: Busca) -> None:
    """Volta uma busca com erro para a fila de processamento, usando os resultados já guardados."""
    guardadas = db.scalar(select(func.count()).select_from(Empresa).where(Empresa.busca_id == busca.id))
    if busca.status != BuscaStatus.ERRO or not (busca.apify_dataset_id or guardadas):
        raise BuscaError("Só dá para reprocessar uma busca com erro que já tenha resultados no Apify.")
    busca.status = BuscaStatus.EXECUTANDO
    busca.erro = None


def buscas_em_andamento(db: Session) -> list[Busca]:
    return list(
        db.scalars(select(Busca).where(Busca.status == BuscaStatus.EXECUTANDO, Busca.apify_run_id.is_not(None)))
    )
