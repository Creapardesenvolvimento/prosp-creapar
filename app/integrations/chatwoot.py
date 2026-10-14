"""Cliente da API de aplicação do Chatwoot (v4)."""

from collections.abc import Iterator

import httpx

from app.config import get_settings

ACTIVE_STATUSES = ("open", "pending")


class ChatwootError(Exception):
    pass


class ChatwootClient:
    def __init__(self, base_url: str, token: str, account_id: str, transport: httpx.BaseTransport | None = None):
        self._base_url = f"{base_url.rstrip('/')}/api/v1/accounts/{account_id}"
        self._token = token
        self._transport = transport

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers={"api_access_token": self._token},
                timeout=30,
                transport=self._transport,
            ) as http:
                resp = http.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ChatwootError(f"Chatwoot inacessível: {exc}") from exc
        if resp.status_code >= 400:
            raise ChatwootError(f"Chatwoot respondeu {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def find_inbox_id(self, name: str) -> int | None:
        for inbox in self._request("GET", "/inboxes")["payload"]:
            if inbox["name"] == name:
                return inbox["id"]
        return None

    def ensure_label(self, title: str) -> None:
        """Garante que a label existe na conta (senão ela não aparece na interface)."""
        existing = {label["title"] for label in self._request("GET", "/labels")["payload"]}
        if title not in existing:
            self._request("POST", "/labels", json={"title": title, "show_on_sidebar": True})

    def active_conversations(self, inbox_id: int) -> Iterator[dict]:
        page = 1
        while True:
            data = self._request(
                "GET", "/conversations", params={"inbox_id": inbox_id, "status": "all", "page": page}
            )
            conversations = data["data"]["payload"]
            if not conversations:
                return
            for conversation in conversations:
                if conversation.get("status") in ACTIVE_STATUSES:
                    yield conversation
            page += 1

    def conversas_do_telefone(self, telefone: str, inbox_id: int) -> list[dict]:
        """Conversas do contato (pelo telefone E.164) numa caixa de entrada, da mais recente para a mais antiga."""
        contatos = self._request("GET", "/contacts/search", params={"q": telefone.lstrip("+")})["payload"]
        conversas = []
        for contato in contatos:
            if (contato.get("phone_number") or "").lstrip("+") != telefone.lstrip("+"):
                continue
            for conversa in self._request("GET", f"/contacts/{contato['id']}/conversations")["payload"]:
                if conversa.get("inbox_id") == inbox_id:
                    conversas.append(conversa)
        return sorted(conversas, key=lambda c: c.get("last_activity_at") or c.get("id") or 0, reverse=True)

    def add_label(self, conversation: dict, label: str) -> bool:
        """Acrescenta a label preservando as existentes (o endpoint substitui a lista)."""
        labels = list(conversation.get("labels") or [])
        if label in labels:
            return False
        self._request("POST", f"/conversations/{conversation['id']}/labels", json={"labels": labels + [label]})
        return True


def get_client() -> ChatwootClient:
    settings = get_settings()
    return ChatwootClient(settings.chatwoot_base_url, settings.chatwoot_token, settings.chatwoot_account_id)
