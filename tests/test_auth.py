def test_health_e_publico(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_painel_exige_login(client):
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_painel_exige_login_htmx(client):
    resp = client.get("/", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


def test_login_senha_errada(client, user):
    resp = client.post("/login", data={"email": user.email, "senha": "errada"})
    assert resp.status_code == 401
    assert client.get("/").status_code == 303


def test_login_e_logout(logged_client):
    resp = logged_client.get("/")
    assert resp.status_code == 200
    assert "Olá, Time" in resp.text

    assert logged_client.post("/logout").status_code == 303
    assert logged_client.get("/").status_code == 303


def test_usuario_inativo_perde_acesso(logged_client, user, db):
    db.get(type(user), user.id).ativo = False
    db.commit()
    assert logged_client.get("/").status_code == 303
