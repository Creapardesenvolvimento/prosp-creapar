"""Cliente da Evolution API v2 (payloads conferidos contra a v2.3.7)."""

from urllib.parse import quote

import httpx

from app.models import EvolutionServer


class EvolutionError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"Evolution respondeu {status}: {message}" if status else f"Evolution inacessível: {message}")
        self.status = status


def _error_message(resp: httpx.Response) -> str:
    try:
        message = resp.json()["response"]["message"]
    except (ValueError, KeyError, TypeError):
        return resp.text[:200]
    while isinstance(message, list) and message:
        message = message[0]
    return str(message)


class EvolutionClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport

    def _request(self, method: str, path: str, json: dict | None = None):
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers={"apikey": self._api_key},
                timeout=30,
                transport=self._transport,
            ) as http:
                resp = http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise EvolutionError(None, str(exc)) from exc
        if resp.status_code >= 400:
            raise EvolutionError(resp.status_code, _error_message(resp))
        return resp.json()

    def create_instance(self, instance_name: str) -> dict:
        """Cria a instância Baileys já pedindo o QR. O QR vem em ["qrcode"]["base64"]."""
        return self._request(
            "POST",
            "/instance/create",
            {"instanceName": instance_name, "integration": "WHATSAPP-BAILEYS", "qrcode": True},
        )

    def connect(self, instance_name: str) -> dict:
        """Devolve um QR novo ({"base64": ...}) enquanto a instância não está conectada."""
        return self._request("GET", f"/instance/connect/{quote(instance_name)}")

    def connection_state(self, instance_name: str) -> str:
        """open | connecting | close"""
        data = self._request("GET", f"/instance/connectionState/{quote(instance_name)}")
        return data["instance"]["state"]

    def fetch_instance(self, instance_name: str) -> dict:
        data = self._request("GET", f"/instance/fetchInstances?instanceName={quote(instance_name)}")
        return data[0]

    def set_webhook(self, instance_name: str, url: str, events: list[str]) -> dict:
        return self._request(
            "POST",
            f"/webhook/set/{quote(instance_name)}",
            {"webhook": {"enabled": True, "url": url, "byEvents": False, "base64": False, "events": events}},
        )

    def set_chatwoot(self, instance_name: str, *, url: str, account_id: str, token: str, name_inbox: str) -> dict:
        return self._request(
            "POST",
            f"/chatwoot/set/{quote(instance_name)}",
            {
                "enabled": True,
                "accountId": str(account_id),
                "token": token,
                "url": url.rstrip("/"),
                "nameInbox": name_inbox,
                "signMsg": False,
                "autoCreate": True,
                "importContacts": False,
                "importMessages": False,
                "daysLimitImportMessages": 0,
                "reopenConversation": True,
                "conversationPending": False,
                "mergeBrazilContacts": True,
            },
        )

    def send_text(self, instance_name: str, number: str, text: str, delay_ms: int = 0) -> dict:
        """number só com dígitos (5511999998888). delay_ms mostra "digitando…" antes de enviar.

        O id da mensagem vem em ["key"]["id"].
        """
        body = {"number": number, "text": text}
        if delay_ms:
            body["delay"] = delay_ms
        return self._request("POST", f"/message/sendText/{quote(instance_name)}", body)

    def check_whatsapp(self, instance_name: str, numbers: list[str]) -> dict[str, str | None]:
        """Para cada número (só dígitos), o jid se ele tem WhatsApp, ou None se não tem."""
        data = self._request("POST", f"/chat/whatsappNumbers/{quote(instance_name)}", {"numbers": numbers})
        resultado: dict[str, str | None] = dict.fromkeys(numbers)
        for item in data:
            if item.get("number") in resultado:
                resultado[item["number"]] = item.get("jid") if item.get("exists") else None
        return resultado

    def logout(self, instance_name: str) -> dict:
        return self._request("DELETE", f"/instance/logout/{quote(instance_name)}")

    def delete_instance(self, instance_name: str) -> dict:
        return self._request("DELETE", f"/instance/delete/{quote(instance_name)}")


def get_client(server: EvolutionServer) -> EvolutionClient:
    return EvolutionClient(server.base_url, server.api_key)
