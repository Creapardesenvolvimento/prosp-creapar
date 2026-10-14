"""Cliente da API do Apify para o actor de Google Maps."""

import base64
import json
from collections.abc import Iterator

import httpx

from app.config import get_settings

API_URL = "https://api.apify.com/v2"
FIM_DE_RUN = ["ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.FAILED", "ACTOR.RUN.ABORTED", "ACTOR.RUN.TIMED_OUT"]


class ApifyError(Exception):
    pass


class ApifyClient:
    def __init__(self, token: str, actor_id: str, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._actor_id = actor_id
        self._transport = transport

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None):
        if not self._token:
            raise ApifyError("APIFY_TOKEN não configurado.")
        try:
            with httpx.Client(
                base_url=API_URL,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=60,
                transport=self._transport,
            ) as http:
                resp = http.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise ApifyError(f"Apify inacessível: {exc}") from exc
        if resp.status_code >= 400:
            try:
                message = resp.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = resp.text[:200]
            raise ApifyError(f"Apify respondeu {resp.status_code}: {message}")
        return resp.json()

    def start_run(self, run_input: dict, *, webhook_url: str, max_total_charge_usd: float) -> dict:
        """Dispara o actor e pede um webhook no fim do run. Retorna o objeto do run (id, status, defaultDatasetId)."""
        webhooks = [{"eventTypes": FIM_DE_RUN, "requestUrl": webhook_url}]
        params = {
            "webhooks": base64.b64encode(json.dumps(webhooks).encode()).decode(),
            "maxTotalChargeUsd": f"{max_total_charge_usd:.2f}",  # teto de gasto do run
        }
        return self._request("POST", f"/acts/{self._actor_id}/runs", params=params, json_body=run_input)["data"]

    def get_run(self, run_id: str) -> dict:
        return self._request("GET", f"/actor-runs/{run_id}")["data"]

    def dataset_items(self, dataset_id: str, page_size: int = 1000) -> Iterator[dict]:
        offset = 0
        while True:
            items = self._request(
                "GET",
                f"/datasets/{dataset_id}/items",
                params={"clean": "true", "format": "json", "offset": offset, "limit": page_size},
            )
            yield from items
            if len(items) < page_size:
                return
            offset += page_size


def get_client() -> ApifyClient:
    settings = get_settings()
    return ApifyClient(settings.apify_token, settings.apify_actor_id)
