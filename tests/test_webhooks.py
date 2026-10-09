import pytest
from sqlalchemy import func, select

from app.api import webhooks
from app.models import WebhookEvent

URL = "/webhooks/evolution/test-webhook-token"
PAYLOAD = {"event": "connection.update", "instance": "chip-1", "data": {"state": "open"}}


@pytest.fixture
def calls(monkeypatch):
    received = []
    monkeypatch.setitem(webhooks.EVOLUTION_HANDLERS, "connection.update", lambda db, p: received.append(p))
    return received


def test_token_errado_retorna_404(client):
    assert client.post("/webhooks/evolution/errado", json=PAYLOAD).status_code == 404


def test_json_invalido(client):
    assert client.post(URL, content=b"nao-json").status_code == 400


def test_evento_chega_no_handler(client, calls):
    resp = client.post(URL, json=PAYLOAD)
    assert resp.json() == {"status": "ok"}
    assert calls == [PAYLOAD]


def test_nome_do_evento_em_maiusculas(client, calls):
    client.post(URL, json={**PAYLOAD, "event": "CONNECTION_UPDATE"})
    assert len(calls) == 1


def test_reenvio_e_ignorado(client, calls, db):
    client.post(URL, json=PAYLOAD)
    resp = client.post(URL, json=PAYLOAD)
    assert resp.json() == {"status": "duplicate"}
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_falha_no_handler_permite_reprocessar(client, monkeypatch, db):
    def boom(db, payload):
        raise RuntimeError("falhou")

    monkeypatch.setitem(webhooks.EVOLUTION_HANDLERS, "connection.update", boom)
    with pytest.raises(RuntimeError):
        client.post(URL, json=PAYLOAD)
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0
