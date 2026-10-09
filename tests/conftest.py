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
)

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.integrations import chatwoot, evolution
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

    def logout(self, name):
        self._call("logout", name)

    def delete_instance(self, name):
        self._call("delete_instance", name)


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(engine)
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
