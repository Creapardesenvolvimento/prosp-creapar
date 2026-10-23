import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="creapar-tests-"))

# Valores fixos de teste: nada do .env real pode vazar para cá.
os.environ.update(
    ENVIRONMENT="test",
    SECRET_KEY="test-secret-key",
    WEBHOOK_TOKEN="test-webhook-token",
    PUBLIC_BASE_URL="https://painel.test",
    DATABASE_URL=f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    CELERY_ALWAYS_EAGER="true",
    TIMEZONE="America/Sao_Paulo",
    RAMPA_LIMITES="1:10,4:20,8:40",
    EVOLUTION_BASE_URL="https://evolution.test",
    EVOLUTION_API_KEY="evo-key",
    CHATWOOT_BASE_URL="https://chatwoot.test",
    CHATWOOT_TOKEN="cw-token",
    CHATWOOT_ACCOUNT_ID="1",
    APIFY_TOKEN="apify-token",
)

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.integrations import apify, chatwoot, evolution
from app.main import app
from app.models import EvolutionServer, User
from app.security import hash_password


class FakeChatwoot:
    def __init__(self):
        self.inboxes: dict[str, int] = {}
        self.labels: set[str] = set()
        self.conversations: list[dict] = []
        self.fail: Exception | None = None

    def find_inbox_id(self, name):
        if self.fail:
            raise self.fail
        return self.inboxes.get(name)

    def ensure_label(self, title):
        self.labels.add(title)

    def active_conversations(self, inbox_id):
        return [c for c in self.conversations if c["inbox_id"] == inbox_id and c["status"] in ("open", "pending")]

    def conversas_do_telefone(self, telefone, inbox_id):
        return [c for c in self.conversations if c.get("telefone") == telefone and c["inbox_id"] == inbox_id]

    def add_label(self, conversation, label):
        if label in conversation["labels"]:
            return False
        conversation["labels"].append(label)
        return True


class FakeEvolution:
    def __init__(self, chatwoot_fake: FakeChatwoot):
        self.chatwoot = chatwoot_fake
        self.calls: list[tuple] = []
        self.state = "connecting"
        self.owner_jid = "5511999990001@s.whatsapp.net"
        self.fail: dict[str, Exception] = {}
        self.sem_whatsapp: set[str] = set()

    def _call(self, method, *args):
        self.calls.append((method, *args))
        if method in self.fail:
            raise self.fail[method]

    def called(self, method):
        return [c for c in self.calls if c[0] == method]

    def create_instance(self, name):
        self._call("create_instance", name)
        return {"qrcode": {"base64": "data:image/png;base64,QR1"}}

    def connect(self, name):
        self._call("connect", name)
        return {"base64": "data:image/png;base64,QR2"}

    def connection_state(self, name):
        self._call("connection_state", name)
        return self.state

    def fetch_instance(self, name):
        self._call("fetch_instance", name)
        return {"ownerJid": self.owner_jid}

    def set_webhook(self, name, url, events):
        self._call("set_webhook", name, url, events)

    def set_chatwoot(self, name, **kwargs):
        self._call("set_chatwoot", name, kwargs)
        self.chatwoot.inboxes[kwargs["name_inbox"]] = 70 + len(self.chatwoot.inboxes)

    def send_text(self, name, number, text, delay_ms=0):
        self._call("send_text", name, number, text)
        return {"key": {"id": f"MSG{len(self.calls)}", "fromMe": True}}

    def check_whatsapp(self, name, numbers):
        self._call("check_whatsapp", name, list(numbers))
        return {n: None if n in self.sem_whatsapp else f"{n}@s.whatsapp.net" for n in numbers}

    def set_proxy(self, name, **kwargs):
        self._call("set_proxy", name, kwargs)
        return {}

    def disable_proxy(self, name):
        self._call("disable_proxy", name)
        return {}

    def logout(self, name):
        self._call("logout", name)

    def delete_instance(self, name):
        self._call("delete_instance", name)


class FakeApify:
    def __init__(self):
        self.calls: list[tuple] = []
        self.items: list[dict] = []
        self.status = "RUNNING"
        self.fail: Exception | None = None

    def start_run(self, run_input, *, webhook_url, max_total_charge_usd):
        self.calls.append(("start_run", run_input, webhook_url, max_total_charge_usd))
        if self.fail:
            raise self.fail
        return {"id": f"run{len(self.calls)}", "defaultDatasetId": f"ds{len(self.calls)}", "status": "RUNNING"}

    def get_run(self, run_id):
        self.calls.append(("get_run", run_id))
        return {"id": run_id, "status": self.status, "defaultDatasetId": "ds1"}

    def dataset_items(self, dataset_id):
        self.calls.append(("dataset_items", dataset_id))
        return iter(self.items)


@pytest.fixture(autouse=True)
def apify_fake(monkeypatch):
    fake = FakeApify()
    monkeypatch.setattr(apify, "get_client", lambda: fake)
    return fake


@pytest.fixture
def conectado(db, server, evo):
    """Um número de WhatsApp conectado (usado para validar telefones e mandar avisos)."""
    from app.services import numeros

    numero, _ = numeros.criar_numero(db, "Chip conectado", server)
    numeros.aplicar_estado(db, numero, "open")
    db.commit()
    return numero


CLIENTE_PADRAO = 1


@pytest.fixture(autouse=True)
def _schema():
    from app.models import Cliente

    Base.metadata.create_all(engine)
    with SessionLocal() as s:  # como na migração: tudo começa no cliente Brafort10
        s.add(Cliente(id=CLIENTE_PADRAO, nome="Brafort10"))
        s.commit()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def cw(monkeypatch):
    fake = FakeChatwoot()
    monkeypatch.setattr(chatwoot, "get_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def evo(monkeypatch, cw):
    fake = FakeEvolution(cw)
    monkeypatch.setattr(evolution, "get_client", lambda server: fake)
    return fake


@pytest.fixture
def db():
    with SessionLocal() as session:
        session.info["cliente_id"] = CLIENTE_PADRAO  # os testes trabalham no cliente padrão, como o painel
        yield session


@pytest.fixture
def server(db):
    server = EvolutionServer(nome="Principal", base_url="https://evolution.test", api_key="evo-key")
    db.add(server)
    db.commit()
    return server


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def user(db):
    user = User(email="time@creapar.com", nome="Time", senha_hash=hash_password("senha-de-teste"))
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def logged_client(client, user):
    resp = client.post("/login", data={"email": user.email, "senha": "senha-de-teste"})
    assert resp.status_code == 303
    return client


def cadastrar_instancia(db, server, instance_name: str):
    """Número com o nome de instância que os payloads de teste usam: o webhook descobre o cliente por ele."""
    from app.models import NumeroStatus, WhatsappNumber

    # Desconectado: só identifica o cliente, não entra no rodízio de envios dos testes.
    numero = WhatsappNumber(server=server, nome_exibicao=instance_name, instance_name=instance_name, status=NumeroStatus.DESCONECTADO)
    db.add(numero)
    db.commit()
    return numero
