import json

import httpx
import pytest

from app.integrations.chatwoot import ChatwootClient
from app.integrations.evolution import EvolutionClient, EvolutionError


def evolution_with(handler):
    return EvolutionClient("https://evolution.test/", "evo-key", transport=httpx.MockTransport(handler))


def test_evolution_create_instance_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["apikey"] = request.headers["apikey"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"qrcode": {"base64": "data:image/png;base64,AAA"}})

    data = evolution_with(handler).create_instance("creapar-x")
    assert seen == {
        "url": "https://evolution.test/instance/create",
        "apikey": "evo-key",
        "body": {"instanceName": "creapar-x", "integration": "WHATSAPP-BAILEYS", "qrcode": True},
    }
    assert data["qrcode"]["base64"].startswith("data:image/png")


def test_evolution_connection_state():
    def handler(request):
        assert request.url.path == "/instance/connectionState/creapar-x"
        return httpx.Response(200, json={"instance": {"instanceName": "creapar-x", "state": "open"}})

    assert evolution_with(handler).connection_state("creapar-x") == "open"


def test_evolution_set_webhook_payload():
    def handler(request):
        assert request.url.path == "/webhook/set/creapar-x"
        assert json.loads(request.content) == {
            "webhook": {
                "enabled": True,
                "url": "https://painel.test/hook",
                "byEvents": False,
                "base64": False,
                "events": ["CONNECTION_UPDATE"],
            }
        }
        return httpx.Response(201, json={"enabled": True})

    evolution_with(handler).set_webhook("creapar-x", "https://painel.test/hook", ["CONNECTION_UPDATE"])


def test_evolution_set_chatwoot_payload():
    def handler(request):
        assert request.url.path == "/chatwoot/set/creapar-x"
        body = json.loads(request.content)
        assert body["nameInbox"] == "Chip 1"
        assert body["accountId"] == "1"
        assert body["url"] == "https://chatwoot.test"
        assert (body["signMsg"], body["autoCreate"], body["importContacts"], body["importMessages"]) == (
            False,
            True,
            False,
            False,
        )
        # exigidos pela validação da v2.3.7
        assert {"enabled", "token", "reopenConversation", "conversationPending"} <= body.keys()
        return httpx.Response(201, json={"enabled": True})

    evolution_with(handler).set_chatwoot(
        "creapar-x", url="https://chatwoot.test/", account_id=1, token="t", name_inbox="Chip 1"
    )


def test_evolution_erro_http_vira_evolution_error():
    def handler(request):
        return httpx.Response(
            404, json={"status": 404, "error": "Not Found", "response": {"message": ["instance does not exist"]}}
        )

    with pytest.raises(EvolutionError) as exc:
        evolution_with(handler).connection_state("sumiu")
    assert exc.value.status == 404
    assert "instance does not exist" in str(exc.value)


def test_evolution_erro_de_rede_vira_evolution_error():
    def handler(request):
        raise httpx.ConnectError("sem rede")

    with pytest.raises(EvolutionError) as exc:
        evolution_with(handler).connection_state("x")
    assert exc.value.status is None


def chatwoot_with(handler):
    return ChatwootClient("https://chatwoot.test", "cw-token", "1", transport=httpx.MockTransport(handler))


def test_chatwoot_find_inbox_id():
    def handler(request):
        assert request.url.path == "/api/v1/accounts/1/inboxes"
        assert request.headers["api_access_token"] == "cw-token"
        return httpx.Response(200, json={"payload": [{"id": 3, "name": "Outro"}, {"id": 9, "name": "Chip 1"}]})

    client = chatwoot_with(handler)
    assert client.find_inbox_id("Chip 1") == 9
    assert client.find_inbox_id("Nao existe") is None


def test_chatwoot_active_conversations_pagina_e_filtra_resolvidas():
    pages = {
        "1": [{"id": 1, "status": "open"}, {"id": 2, "status": "resolved"}],
        "2": [{"id": 3, "status": "pending"}],
        "3": [],
    }

    def handler(request):
        assert request.url.params["inbox_id"] == "9"
        return httpx.Response(200, json={"data": {"payload": pages[request.url.params["page"]]}})

    assert [c["id"] for c in chatwoot_with(handler).active_conversations(9)] == [1, 3]


def test_chatwoot_add_label_preserva_as_existentes():
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"payload": []})

    client = chatwoot_with(handler)
    assert client.add_label({"id": 5, "labels": ["vip"]}, "numero-perdido") is True
    assert client.add_label({"id": 6, "labels": ["numero-perdido"]}, "numero-perdido") is False
    assert sent == [("/api/v1/accounts/1/conversations/5/labels", {"labels": ["vip", "numero-perdido"]})]


def test_chatwoot_ensure_label_cria_so_se_faltar():
    created = []

    def handler(request):
        if request.method == "POST":
            created.append(json.loads(request.content)["title"])
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"payload": [{"title": "vip"}]})

    client = chatwoot_with(handler)
    client.ensure_label("vip")
    client.ensure_label("numero-perdido")
    assert created == ["numero-perdido"]
