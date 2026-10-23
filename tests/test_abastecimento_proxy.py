import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.config import get_settings
from app.integrations.evolution import EvolutionClient, EvolutionError
from app.models import Busca, BuscaStatus, Lead, LeadStatus, PlanoBusca
from app.services import abastecimento, config_sistema, numeros
from app.services.abastecimento import AbastecimentoError
from app.services.numeros import NumeroError
from tests.test_disparo import SEGUNDA_9H, criar_campanha, criar_leads, criar_numero


@pytest.fixture
def apify_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_token", "token-de-teste")


@pytest.fixture
def plano(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    abastecimento.adicionar_ao_plano(db, termo="oficina mecânica", cidades="Campinas, SP\nSorocaba, SP", limite=200, campanha=c)
    config_sistema.salvar(db, {"abastecer_ativo": "1", "meta_diaria": "10", "abastecer_estoque_dias": "2"})
    db.commit()
    return c


# --- plano de buscas ------------------------------------------------------------------


def test_adicionar_ao_plano(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    assert abastecimento.adicionar_ao_plano(db, termo="padaria", cidades="Campinas\n campinas \nJundiaí; Itu", limite=100, campanha=c) == 3
    assert abastecimento.adicionar_ao_plano(db, termo="Padaria", cidades="Campinas", limite=100, campanha=c) == 0
    with pytest.raises(AbastecimentoError, match="campanha"):
        abastecimento.adicionar_ao_plano(db, termo="padaria", cidades="Itu", limite=100, campanha=None)
    with pytest.raises(AbastecimentoError, match="entre 1 e"):
        abastecimento.adicionar_ao_plano(db, termo="padaria", cidades="Itu", limite=9999, campanha=c)
    with pytest.raises(AbastecimentoError, match="cidade"):
        abastecimento.adicionar_ao_plano(db, termo="padaria", cidades=" \n ", limite=100, campanha=c)


def test_abastece_quando_a_fila_baixa(db, plano, apify_token):
    assert abastecimento.alvo_de_estoque(db) == 20
    busca = abastecimento.abastecer(db, SEGUNDA_9H)
    assert (busca.termo, busca.cidade, busca.limite, busca.campanha_id) == ("oficina mecânica", "Campinas, SP", 200, plano.id)
    item = db.scalar(select(PlanoBusca).where(PlanoBusca.cidade == "Campinas, SP"))
    assert item.busca_id == busca.id and item.buscado_em is not None

    assert abastecimento.abastecer(db, SEGUNDA_9H) is None  # uma busca por vez
    busca.status = BuscaStatus.CONCLUIDA
    criar_leads(db, plano, 25)
    assert abastecimento.abastecer(db, SEGUNDA_9H) is None  # fila acima do alvo

    db.execute(text("update leads set status = 'enviado'"))
    proxima = abastecimento.abastecer(db, SEGUNDA_9H)
    assert proxima.cidade == "Sorocaba, SP"
    proxima.status = BuscaStatus.CONCLUIDA
    assert abastecimento.abastecer(db, SEGUNDA_9H) is None  # plano acabou


@pytest.mark.parametrize("cenario", ["desligado", "sem_token", "teto"])
def test_nao_abastece(db, plano, monkeypatch, cenario):
    monkeypatch.setattr(get_settings(), "apify_token", "" if cenario == "sem_token" else "token-de-teste")
    if cenario == "desligado":
        config_sistema.salvar(db, {"abastecer_ativo": ""})
    elif cenario == "teto":
        config_sistema.salvar(db, {"abastecer_gasto_diario": "0.5"})  # uma busca de 200 custa até US$ 0,93
    assert abastecimento.abastecer(db, SEGUNDA_9H) is None
    assert db.scalar(select(Busca)) is None


def test_pula_busca_feita_na_mao(db, plano, apify_token):
    from app.services import buscas

    manual = buscas.criar_busca(db, "Oficina Mecânica", "campinas, sp", 50)
    manual.status = BuscaStatus.CONCLUIDA
    db.commit()
    busca = abastecimento.abastecer(db)
    assert busca.cidade == "Sorocaba, SP"
    assert db.scalar(select(PlanoBusca.busca_id).where(PlanoBusca.cidade == "Campinas, SP")) == manual.id


def test_task_de_abastecimento_inicia_no_apify(db, plano, apify_token, apify_fake):
    from app.tasks.buscas import abastecer

    busca_id = abastecer()
    assert busca_id and apify_fake.calls[0][0] == "start_run"
    db.expire_all()
    assert db.get(Busca, busca_id).status == BuscaStatus.EXECUTANDO


def test_leads_da_busca_automatica_entram_na_campanha(db, plano, apify_token, apify_fake):
    from app.services import buscas

    busca = abastecimento.abastecer(db)
    busca.status, busca.apify_dataset_id = BuscaStatus.PROCESSANDO, "ds1"
    apify_fake.items = [{"title": "Oficina do Zé", "phoneUnformatted": "+5519955550001", "placeId": "p1"}]
    buscas.processar(db, busca, pausa_entre_lotes=0)
    lead = db.scalar(select(Lead).where(Lead.busca_id == busca.id))
    assert (lead.campanha_id, lead.status) == (plano.id, LeadStatus.NA_FILA)


def test_tela_do_plano(logged_client, db, server, evo, monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_token", "")
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    resp = logged_client.post("/prospeccao/plano", data={"termo": "padaria", "cidades": "Campinas\nItu", "limite": "100", "campanha_id": str(c.id)})
    assert "ok=" in resp.headers["location"]
    pagina = logged_client.get("/prospeccao").text
    assert "Plano de buscas" in pagina and "padaria — Itu" in pagina and "aguardando" in pagina
    resp = logged_client.post("/prospeccao/plano", data={"termo": "padaria", "cidades": "Jundiaí", "limite": "100", "campanha_id": ""})
    assert "erro=" in resp.headers["location"]

    resp = logged_client.post("/prospeccao/abastecimento", data={"abastecer_ativo": "1", "abastecer_estoque_dias": "3", "abastecer_gasto_diario": "15"})
    assert "ok=" in resp.headers["location"]
    from app.db import SessionLocal

    with SessionLocal() as s:
        s.info["cliente_id"] = 1  # sessão de conferência, no cliente padrão
        v = config_sistema.valores(s)
        assert (v["abastecer_ativo"], v["abastecer_estoque_dias"], v["abastecer_gasto_diario"]) == (True, 3.0, 15.0)
        item_id = s.scalar(select(PlanoBusca.id).where(PlanoBusca.cidade == "Itu"))
    logged_client.post(f"/prospeccao/plano/{item_id}/remover")
    assert "padaria — Itu" not in logged_client.get("/prospeccao").text
    assert "erro=" in logged_client.post("/prospeccao/abastecer-agora").headers["location"]  # sem token do Apify


# --- filas do Celery -----------------------------------------------------------------


def test_ia_e_disparo_em_filas_proprias():
    from app.celery_app import celery_app

    rota = celery_app.amqp.router.route
    assert rota({}, "agente.responder")["queue"].name == "ia"
    assert rota({}, "disparo.ciclo")["queue"].name == "disparo"
    assert rota({}, "buscas.processar")["queue"].name == "celery"
    with open("start.sh", encoding="utf-8") as f:
        assert '-Q "${WORKER_QUEUES:-celery,ia,disparo}"' in f.read()


# --- proxy por número ------------------------------------------------------------------


def test_evolution_proxy_payloads():
    corpos = []

    def handler(request):
        assert request.url.path == "/proxy/set/chip"
        corpos.append(json.loads(request.content))
        return httpx.Response(201, json={})

    client = EvolutionClient("https://evolution.test", "k", transport=httpx.MockTransport(handler))
    client.set_proxy("chip", protocol="socks5", host="1.2.3.4", port="1080", username="u", password="p")
    client.disable_proxy("chip")
    assert corpos == [
        {"enabled": True, "host": "1.2.3.4", "port": "1080", "protocol": "socks5", "username": "u", "password": "p"},
        {"enabled": False, "host": "", "port": "", "protocol": "", "username": "", "password": ""},
    ]


def test_definir_e_remover_proxy(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    numeros.definir_proxy(db, n, protocolo="HTTP", host=" proxy.exemplo.com ", porta="8080", usuario="cliente", senha="s3gredo")
    db.commit()
    (_, instance, dados), = evo.called("set_proxy")
    assert instance == n.instance_name
    assert dados == {"protocol": "http", "host": "proxy.exemplo.com", "port": "8080", "username": "cliente", "password": "s3gredo"}
    bruto = db.execute(text("select proxy_senha from whatsapp_numbers where id = :id"), {"id": n.id}).scalar()
    assert bruto and "s3gredo" not in bruto  # senha criptografada no banco
    assert n.tem_proxy and n.proxy_senha == "s3gredo"

    numeros.definir_proxy(db, n, protocolo="socks5", host="proxy.exemplo.com", porta="1080", usuario="cliente", senha="")
    assert evo.called("set_proxy")[-1][2]["password"] == "s3gredo"  # senha em branco mantém a atual

    numeros.remover_proxy(db, n)
    assert evo.called("disable_proxy") and not n.tem_proxy and n.proxy_senha is None


@pytest.mark.parametrize(
    "campos, erro",
    [
        ({"protocolo": "https"}, "http ou socks5"),
        ({"host": "http://proxy.com"}, "sem http"),
        ({"porta": "70000"}, "Porta"),
        ({"usuario": "u", "senha": ""}, "usuário e senha"),
        ({"usuario": "a@b", "senha": "x"}, "não podem ter"),
    ],
)
def test_proxy_validado(db, server, evo, campos, erro):
    n = criar_numero(db, server, "Chip", 10)
    dados = {"protocolo": "http", "host": "proxy.com", "porta": "8080", "usuario": "", "senha": "", **campos}
    with pytest.raises(NumeroError, match=erro):
        numeros.definir_proxy(db, n, **dados)
    assert evo.called("set_proxy") == [] and not n.tem_proxy


def test_proxy_recusado_pela_evolution(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    evo.fail["set_proxy"] = EvolutionError(400, "Invalid proxy")
    with pytest.raises(NumeroError, match="testou o proxy"):
        numeros.definir_proxy(db, n, protocolo="http", host="proxy.com", porta="8080")
    assert not n.tem_proxy


def test_tela_do_proxy(logged_client, db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    assert "Ligar proxy" in logged_client.get(f"/numeros/{n.id}").text
    resp = logged_client.post(f"/numeros/{n.id}/proxy", data={"protocolo": "http", "host": "proxy.com", "porta": "8080"})
    assert "erro=" not in resp.headers["location"]
    assert "Atualizar proxy" in logged_client.get(f"/numeros/{n.id}").text
    assert "proxy.com" in logged_client.get("/numeros").text
    resp = logged_client.post(f"/numeros/{n.id}/proxy", data={"protocolo": "http", "host": "proxy.com", "porta": "abc"})
    assert "erro=" in resp.headers["location"]
    logged_client.post(f"/numeros/{n.id}/proxy/remover")
    assert "Ligar proxy" in logged_client.get(f"/numeros/{n.id}").text
