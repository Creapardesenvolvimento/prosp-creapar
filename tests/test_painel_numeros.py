from sqlalchemy import select

from app.db import SessionLocal
from app.models import NumeroStatus, WhatsappNumber

WEBHOOK = "/webhooks/evolution/test-webhook-token"


def criar(logged_client, server, nome="Chip 1", **extra):
    resp = logged_client.post("/numeros", data={"nome_exibicao": nome, "server_id": server.id, **extra})
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


def get_numero(db, numero_id):
    # Sessão própria e curta: uma leitura aberta na fixture `db` travaria o SQLite para a requisição seguinte.
    with SessionLocal() as session:
        return session.get(WhatsappNumber, numero_id)


def test_telas_exigem_login(client):
    for path in ("/numeros", "/numeros/novo", "/numeros/1", "/numeros/1/qr", "/servidores"):
        assert client.get(path).status_code == 303, path
    assert client.post("/numeros/1/remover").status_code == 303
    assert client.post("/numeros/1/remover").headers["location"] == "/login"


def test_form_novo_cadastra_o_servidor_do_env(logged_client):
    resp = logged_client.get("/numeros/novo")
    assert resp.status_code == 200
    assert "Principal" in resp.text


def test_fluxo_de_criacao_ate_conectar(logged_client, server, db, evo):
    numero_id = criar(logged_client, server)

    detalhe = logged_client.get(f"/numeros/{numero_id}")
    assert "aguardando qr" in detalhe.text
    assert f"/numeros/{numero_id}/qr" in detalhe.text

    qr = logged_client.get(f"/numeros/{numero_id}/qr")
    assert "data:image/png;base64,QR2" in qr.text
    assert "HX-Refresh" not in qr.headers

    evo.state = "open"
    qr = logged_client.get(f"/numeros/{numero_id}/qr")
    assert qr.headers["HX-Refresh"] == "true"
    assert get_numero(db, numero_id).status == NumeroStatus.CONECTADO

    lista = logged_client.get("/numeros")
    assert "+5511999990001" in lista.text
    assert "0 / 10" in lista.text


def test_erro_da_evolution_aparece_no_formulario(logged_client, server, db, evo):
    from app.integrations.evolution import EvolutionError

    evo.fail["create_instance"] = EvolutionError(None, "sem rede")
    resp = logged_client.post("/numeros", data={"nome_exibicao": "Chip 1", "server_id": server.id})
    assert resp.status_code == 400
    assert "Evolution inacessível" in resp.text
    assert db.scalar(select(WhatsappNumber)) is None
    db.rollback()


def test_webhook_connection_update_conecta_e_derruba(logged_client, client, server, db):
    numero_id = criar(logged_client, server)
    instance = get_numero(db, numero_id).instance_name

    def evento(state, n, reason=200):
        return {
            "event": "connection.update",
            "instance": instance,
            "data": {"instance": instance, "state": state, "statusReason": reason},
            "date_time": f"2026-03-10T12:00:0{n}.000Z",
        }

    assert client.post(WEBHOOK, json=evento("open", 1)).json() == {"status": "ok"}
    assert get_numero(db, numero_id).status == NumeroStatus.CONECTADO

    assert client.post(WEBHOOK, json=evento("close", 2, 401)).json() == {"status": "ok"}
    numero = get_numero(db, numero_id)
    assert (numero.status, numero.motivo_desconexao) == (NumeroStatus.DESCONECTADO, 401)

    # reenvio do "open" antigo não reconecta o número que já caiu
    assert client.post(WEBHOOK, json=evento("open", 1)).json() == {"status": "duplicate"}
    assert get_numero(db, numero_id).status == NumeroStatus.DESCONECTADO

    assert "desconectado(s) e fora do rodízio" in logged_client.get("/numeros").text
    assert "desconectado(s) e fora do rodízio" in logged_client.get("/").text


def test_webhook_de_instancia_desconhecida_e_aceito(client):
    payload = {"event": "connection.update", "instance": "de-outro-sistema", "data": {"state": "open"}}
    assert client.post(WEBHOOK, json=payload).status_code == 200


def test_pausar_retomar_pelo_painel(logged_client, server, db, evo):
    numero_id = criar(logged_client, server)
    evo.state = "open"
    logged_client.get(f"/numeros/{numero_id}/qr")

    assert logged_client.post(f"/numeros/{numero_id}/pausar").status_code == 303
    assert get_numero(db, numero_id).status == NumeroStatus.PAUSADO
    logged_client.post(f"/numeros/{numero_id}/retomar")
    assert get_numero(db, numero_id).status == NumeroStatus.CONECTADO


def test_acao_invalida_volta_com_erro(logged_client, server):
    numero_id = criar(logged_client, server)
    resp = logged_client.post(f"/numeros/{numero_id}/pausar")
    assert "erro=" in resp.headers["location"]
    assert "pausar um número conectado" in logged_client.get(resp.headers["location"]).text


def test_banir_marca_conversas_e_abre_o_substituto(logged_client, server, db, evo, cw):
    numero_id = criar(logged_client, server)
    evo.state = "open"
    logged_client.get(f"/numeros/{numero_id}/qr")
    inbox = get_numero(db, numero_id).chatwoot_inbox_id
    cw.conversations = [{"id": 1, "inbox_id": inbox, "status": "open", "labels": []}]

    resp = logged_client.post(f"/numeros/{numero_id}/banir")
    assert resp.headers["location"] == f"/numeros/novo?substitui={numero_id}"
    assert get_numero(db, numero_id).status == NumeroStatus.BANIDO
    assert cw.conversations[0]["labels"] == ["numero-perdido"]

    form = logged_client.get(resp.headers["location"])
    assert "Substituir Chip 1" in form.text

    novo_id = criar(logged_client, server, nome="Chip 1 B", substitui_numero_id=numero_id)
    novo = get_numero(db, novo_id)
    assert novo.substitui_numero_id == numero_id
    assert novo.limite_diario_atual == 0
    assert "Substituído por" in logged_client.get(f"/numeros/{numero_id}").text


def test_remover_pelo_painel(logged_client, server, db, evo):
    numero_id = criar(logged_client, server)
    logged_client.post(f"/numeros/{numero_id}/remover")
    assert get_numero(db, numero_id).status == NumeroStatus.REMOVIDO
    assert len(evo.called("delete_instance")) == 1
    assert "Removidos (1)" in logged_client.get("/numeros").text


def test_servidores(logged_client, db):
    resp = logged_client.post(
        "/servidores", data={"nome": "VPS 2", "base_url": "https://evo2.test/", "api_key": "segredo-2"}
    )
    assert resp.status_code == 303
    page = logged_client.get("/servidores")
    assert "VPS 2" in page.text and "https://evo2.test" in page.text
    assert "segredo-2" not in page.text
