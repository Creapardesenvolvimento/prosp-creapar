import base64
import json

import httpx
import pytest
from sqlalchemy import select

from app.integrations.apify import ApifyClient, ApifyError
from app.integrations.evolution import EvolutionClient
from app.models import Blacklist, Busca, BuscaStatus, Lead, LeadStatus
from app.services import buscas as svc
from app.services.buscas import BuscaError


def item(nome, telefone, **extra):
    return {"title": nome, "phoneUnformatted": telefone, "categoryName": "Padaria", "city": "Campinas", **extra}


@pytest.fixture
def busca(db):
    b = svc.criar_busca(db, "padaria", "Campinas, SP", 50)
    b.status = BuscaStatus.PROCESSANDO
    b.apify_run_id, b.apify_dataset_id = "run1", "ds1"
    db.commit()
    return b


# --- criação e início ---------------------------------------------------------


@pytest.mark.parametrize("termo, cidade, limite", [("", "Campinas", 10), ("padaria", " ", 10), ("padaria", "Campinas", 0), ("padaria", "Campinas", 501)])
def test_criar_busca_valida(db, termo, cidade, limite):
    with pytest.raises(BuscaError):
        svc.criar_busca(db, termo, cidade, limite)


def test_custo_maximo():
    assert svc.custo_maximo(10) == 0.5  # mínimo do Apify
    assert svc.custo_maximo(500) == 2.25  # 500 × 0,004 × 1,1 + 0,05


def test_iniciar_dispara_o_actor(db, apify_fake):
    b = svc.criar_busca(db, "clínica odontológica", "Campinas, SP", 200)
    svc.iniciar(db, b)

    (_, entrada, url, teto), = apify_fake.calls
    assert entrada["searchStringsArray"] == ["clínica odontológica"]
    assert entrada["locationQuery"] == "Campinas, SP, Brasil"
    assert entrada["maxCrawledPlacesPerSearch"] == 200
    assert (entrada["countryCode"], entrada["language"]) == ("br", "pt-BR")
    # nada de extras pagos
    assert not entrada["scrapeContacts"] and not entrada["skipClosedPlaces"] and not entrada["scrapePlaceDetailPage"]
    assert url == "https://painel.test/webhooks/apify/test-webhook-token"
    assert teto == svc.custo_maximo(200)
    assert (b.status, b.apify_run_id, b.apify_dataset_id) == (BuscaStatus.EXECUTANDO, "run1", "ds1")


def test_falha_ao_iniciar_vira_erro(db, apify_fake):
    apify_fake.fail = ApifyError("Apify respondeu 401: token inválido")
    b = svc.criar_busca(db, "padaria", "Campinas", 10)
    svc.iniciar(db, b)
    assert b.status == BuscaStatus.ERRO and "401" in b.erro


def test_fim_do_run(db, busca):
    busca.status = BuscaStatus.EXECUTANDO
    assert svc.registrar_fim_do_run(db, busca, {"status": "RUNNING"}) is False
    assert svc.registrar_fim_do_run(db, busca, {"status": "TIMED-OUT", "defaultDatasetId": "ds9"}) is True
    assert busca.apify_dataset_id == "ds9"
    assert svc.registrar_fim_do_run(db, busca, {"status": "FAILED"}) is False
    assert busca.status == BuscaStatus.ERRO
    assert svc.registrar_fim_do_run(db, busca, {"status": "SUCCEEDED"}) is False  # já não está executando


def test_reivindicar_processamento_so_uma_vez(db, busca):
    busca.status = BuscaStatus.EXECUTANDO
    db.commit()
    assert svc.reivindicar_processamento(db, busca.id) is True
    assert svc.reivindicar_processamento(db, busca.id) is False


# --- processamento -------------------------------------------------------------


def test_processar_filtra_deduplica_e_valida(db, busca, conectado, apify_fake, evo):
    db.add(Lead(nome="Antiga", telefone="+5519911110003", status=LeadStatus.ENVIADO))
    db.add(Blacklist(telefone="+5519911110004", motivo="opt-out"))
    db.commit()
    evo.sem_whatsapp = {"5519911110005"}
    apify_fake.items = [
        item("Padaria Boa", "+55 19 91111-0001", website="https://boa.com.br", totalScore=4.7, reviewsCount=120, address="Rua A, 1", placeId="p1"),
        item("Padaria Boa (filial)", "(19) 91111-0001"),  # mesmo telefone
        item("Padaria Dois", "19911110002"),
        item("Já é lead", "+5519911110003"),
        item("Bloqueada", "+5519911110004"),
        item("Sem WhatsApp", "+5519911110005"),
        item("Fixo", "+551933334444"),
        item("Sem telefone", None),
        item("Gringa", "+1 415 555 0100"),
        item("Fechada", "+5519911110006", permanentlyClosed=True),
    ]

    svc.processar(db, busca, pausa_entre_lotes=0)
    db.commit()

    assert busca.status == BuscaStatus.CONCLUIDA
    assert (busca.total_encontrados, busca.total_novos) == (10, 2)
    assert busca.descartes == {
        "repetido_na_busca": 1,
        "ja_era_lead": 1,
        "blacklist": 1,
        "sem_whatsapp": 1,
        "nao_celular": 1,
        "sem_telefone": 2,
        "fechado": 1,
    }
    leads = db.scalars(select(Lead).where(Lead.busca_id == busca.id).order_by(Lead.id)).all()
    assert [l.telefone for l in leads] == ["+5519911110001", "+5519911110002"]
    boa = leads[0]
    assert (boa.nome, boa.status, boa.whatsapp_jid) == ("Padaria Boa", LeadStatus.NOVO, "5519911110001@s.whatsapp.net")
    assert (boa.site, boa.categoria, boa.cidade, boa.nota, boa.avaliacoes, boa.endereco, boa.place_id) == (
        "https://boa.com.br", "Padaria", "Campinas", 4.7, 120, "Rua A, 1", "p1"
    )
    # o lead antigo não foi tocado
    assert db.scalar(select(Lead).where(Lead.telefone == "+5519911110003")).status == LeadStatus.ENVIADO
    # só os candidatos que sobraram foram consultados na Evolution
    (_, _, consultados), = evo.called("check_whatsapp")
    assert consultados == ["5519911110001", "5519911110002", "5519911110005"]


def test_processar_sem_numero_conectado(db, busca, apify_fake):
    apify_fake.items = [item("Padaria", "+5519911110001")]
    with pytest.raises(BuscaError, match="Nenhum número"):
        svc.processar(db, busca, pausa_entre_lotes=0)


def test_processar_sem_candidatos_nao_consulta_evolution(db, busca, apify_fake, evo):
    apify_fake.items = [item("Fixo", "+551933334444")]
    svc.processar(db, busca, pausa_entre_lotes=0)
    assert busca.total_novos == 0 and evo.called("check_whatsapp") == []


def test_validacao_em_lotes_de_50(db, conectado, evo):
    telefones = [f"+55199{i:08d}" for i in range(120)]
    resultado = svc.validar_whatsapp(db, telefones, pausa_entre_lotes=0)
    assert len(resultado) == 120 and all(resultado.values())
    assert [len(c[2]) for c in evo.called("check_whatsapp")] == [50, 50, 20]


def test_reprocessar(db, busca):
    with pytest.raises(BuscaError):
        svc.preparar_reprocessamento(db, busca)  # não está com erro
    svc.marcar_erro(db, busca, "falhou")
    svc.preparar_reprocessamento(db, busca)
    assert (busca.status, busca.erro) == (BuscaStatus.EXECUTANDO, None)


# --- clientes HTTP --------------------------------------------------------------


def test_apify_start_run_pede_webhook_e_teto_de_gasto():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"data": {"id": "r1", "defaultDatasetId": "d1", "status": "READY"}})

    client = ApifyClient("tok", "compass~crawler-google-places", transport=httpx.MockTransport(handler))
    run = client.start_run({"searchStringsArray": ["x"]}, webhook_url="https://painel.test/hook", max_total_charge_usd=1.234)

    assert run["id"] == "r1"
    assert seen["path"] == "/v2/acts/compass~crawler-google-places/runs"
    assert seen["auth"] == "Bearer tok"
    assert seen["params"]["maxTotalChargeUsd"] == "1.23"
    webhooks = json.loads(base64.b64decode(seen["params"]["webhooks"]))
    assert webhooks[0]["requestUrl"] == "https://painel.test/hook"
    assert "ACTOR.RUN.SUCCEEDED" in webhooks[0]["eventTypes"] and "ACTOR.RUN.FAILED" in webhooks[0]["eventTypes"]
    assert seen["body"] == {"searchStringsArray": ["x"]}


def test_apify_dataset_paginado():
    def handler(request):
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=[{"n": i} for i in range(offset, min(offset + 2, 5))])

    client = ApifyClient("tok", "a", transport=httpx.MockTransport(handler))
    assert [i["n"] for i in client.dataset_items("d1", page_size=2)] == [0, 1, 2, 3, 4]


def test_apify_erros():
    client = ApifyClient("tok", "a", transport=httpx.MockTransport(lambda r: httpx.Response(402, json={"error": {"message": "sem saldo"}})))
    with pytest.raises(ApifyError, match="sem saldo"):
        client.get_run("r1")
    with pytest.raises(ApifyError, match="APIFY_TOKEN"):
        ApifyClient("", "a").get_run("r1")


def test_evolution_check_whatsapp():
    def handler(request):
        assert request.url.path == "/chat/whatsappNumbers/chip"
        assert json.loads(request.content) == {"numbers": ["5511999990001", "5511999990002"]}
        return httpx.Response(
            200,
            json=[
                {"jid": "5511999990001@s.whatsapp.net", "exists": True, "number": "5511999990001"},
                {"jid": "5511999990002@s.whatsapp.net", "exists": False, "number": "5511999990002"},
            ],
        )

    client = EvolutionClient("https://evolution.test", "k", transport=httpx.MockTransport(handler))
    assert client.check_whatsapp("chip", ["5511999990001", "5511999990002"]) == {
        "5511999990001": "5511999990001@s.whatsapp.net",
        "5511999990002": None,
    }
