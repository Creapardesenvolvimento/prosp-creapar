import os

os.environ.update(
    ENVIRONMENT="test",
    SECRET_KEY="test-secret-key",
    WEBHOOK_TOKEN="test-webhook-token",
    DATABASE_URL="sqlite://",
    CELERY_ALWAYS_EAGER="true",
)

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import hash_password


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


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
