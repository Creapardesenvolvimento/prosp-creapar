from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Busca, BuscaStatus, Lead
from app.tasks.buscas import conferir_buscas

WEBHOOK = "/webhooks/apify/test-webhook-token"
ITENS = [
    {"title": "Padaria Boa", "phoneUnformatted": "+5519911110001"},
    {"title": "Padaria Dois", "phoneUnformatted": "+5519911110002"},
    {"title": "Fixo", "phoneUnformatted": "+551933334444"},
]


def buscar(busca_id):
    with SessionLocal() as s:
        return s.get(Busca, busca_id)


def total_leads():
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Lead))


def criar(logged_client, **dados):
    resp = logged_client.post("/buscas", data={"termo": "padaria", "cidade": "Campinas, SP", "limite": 50, **dados})
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


def evento(run_id, status="SUCCEEDED", event="ACTOR.RUN.SUCCEEDED"):
    return {"eventType": event, "resource": {"id": run_id, "status": status, "defaultDatasetId": "ds1"}}


def test_telas_exigem_login(client):
    for path in ("/buscas", "/buscas/1", "/buscas/1/status"):
        assert client.get(path).headers["location"] == "/login"


def test_formulario_valida(logged_client):
    resp = logged_client.post("/buscas", data={"termo": "padaria", "cidade": "", "limite": 50})
    assert resp.status_code == 400
    assert "Informe o termo e a cidade" in resp.text


def test_fluxo_completo_pelo_webhook(logged_client, client, conectado, apify_fake):
    apify_fake.items = ITENS
    busca_id = criar(logged_client)
    busca = buscar(busca_id)
    assert (busca.status, busca.apify_run_id) == (BuscaStatus.EXECUTANDO, "run1")

    pagina = logged_client.get(f"/buscas/{busca_id}")
    assert "O Apify está coletando" in pagina.text
    assert "HX-Refresh" not in logged_client.get(f"/buscas/{busca_id}/status").headers

    assert client.post(WEBHOOK, json=evento("run1")).json() == {"status": "ok"}
    busca = buscar(busca_id)
    assert (busca.status, busca.total_encontrados, busca.total_novos) == (BuscaStatus.CONCLUIDA, 3, 2)
    assert total_leads() == 2

    # reenvio do mesmo evento é ignorado
    assert client.post(WEBHOOK, json=evento("run1")).json() == {"status": "duplicate"}
    assert total_leads() == 2

    assert logged_client.get(f"/buscas/{busca_id}/status").headers["HX-Refresh"] == "true"
    pagina = logged_client.get(f"/buscas/{busca_id}")
    assert "Padaria Boa" in pagina.text and "+5519911110002" in pagina.text and "telefone fixo" in pagina.text


def test_webhook_com_token_errado(client):
    assert client.post("/webhooks/apify/errado", json=evento("run1")).status_code == 404


def test_run_que_falhou(logged_client, client, apify_fake):
    busca_id = criar(logged_client)
    client.post(WEBHOOK, json=evento("run1", "FAILED", "ACTOR.RUN.FAILED"))
    busca = buscar(busca_id)
    assert busca.status == BuscaStatus.ERRO and "FAILED" in busca.erro


def test_polling_quando_o_webhook_nao_chega(logged_client, conectado, apify_fake):
    apify_fake.items = ITENS
    busca_id = criar(logged_client)

    conferir_buscas()  # ainda rodando
    assert buscar(busca_id).status == BuscaStatus.EXECUTANDO

    apify_fake.status = "SUCCEEDED"
    conferir_buscas()
    assert buscar(busca_id).status == BuscaStatus.CONCLUIDA
    assert total_leads() == 2


def test_sem_numero_conectado_e_reprocessar(logged_client, client, server, evo, apify_fake):
    apify_fake.items = ITENS
    busca_id = criar(logged_client)
    client.post(WEBHOOK, json=evento("run1"))
    busca = buscar(busca_id)
    assert busca.status == BuscaStatus.ERRO and "Nenhum número" in busca.erro
    assert "Reprocessar resultados" in logged_client.get(f"/buscas/{busca_id}").text

    from app.services import numeros

    with SessionLocal() as s:
        numero, _ = numeros.criar_numero(s, "Chip", s.merge(server))
        numeros.aplicar_estado(s, numero, "open")
        s.commit()

    logged_client.post(f"/buscas/{busca_id}/reprocessar")
    assert buscar(busca_id).status == BuscaStatus.CONCLUIDA
    assert total_leads() == 2


def test_apify_fora_do_ar_ao_criar(logged_client, apify_fake):
    from app.integrations.apify import ApifyError

    apify_fake.fail = ApifyError("Apify inacessível: timeout")
    busca_id = criar(logged_client)
    assert buscar(busca_id).status == BuscaStatus.ERRO
    assert "Apify inacessível" in logged_client.get(f"/buscas/{busca_id}").text
