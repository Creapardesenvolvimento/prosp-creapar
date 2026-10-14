import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Blacklist, Busca, BuscaStatus, Campanha, CampanhaStatus, Lead, LeadStatus
from app.services import campanhas as svc
from app.services import ia
from app.services.campanhas import CampanhaError


@pytest.fixture
def busca(db):
    b = Busca(termo="padaria", cidade="Campinas", limite=10, status=BuscaStatus.CONCLUIDA, descartes={})
    db.add(b)
    db.flush()
    db.add_all(
        [
            Lead(busca_id=b.id, nome="Padaria A", telefone="+5519911110001", cidade="Campinas", status=LeadStatus.NOVO),
            Lead(busca_id=b.id, nome="Padaria B", telefone="+5519911110002", cidade=None, status=LeadStatus.NOVO),
            Lead(busca_id=b.id, nome="Já contatada", telefone="+5519911110003", status=LeadStatus.ENVIADO),
            Lead(busca_id=b.id, nome="Bloqueada", telefone="+5519911110004", status=LeadStatus.NOVO),
        ]
    )
    db.add(Blacklist(telefone="+5519911110004", motivo="opt-out"))
    db.commit()
    return b


@pytest.fixture
def campanha(db):
    c = svc.salvar(db, None, nome="Padarias", nicho="padarias", oferta="mais pedidos", prompt_agente="")
    db.commit()
    return c


def test_validar_texto():
    assert svc.validar_texto("  Oi {empresa} de {cidade}  ") == "Oi {empresa} de {cidade}"
    with pytest.raises(CampanhaError, match="nome"):
        svc.validar_texto("Oi {nome}")
    with pytest.raises(CampanhaError):
        svc.validar_texto("   ")


def test_renderizar():
    lead = Lead(nome=" Padaria A ", telefone="+5519911110001", cidade="Campinas")
    assert svc.renderizar("Oi {empresa}, aí em {cidade}?", lead) == "Oi Padaria A, aí em Campinas?"
    lead.cidade = None
    assert svc.renderizar("Clientes em {cidade}", lead) == "Clientes em sua cidade"


def test_adicionar_leads_so_os_disponiveis(db, busca, campanha):
    assert svc.adicionar_leads(db, campanha, [busca.id]) == 2
    db.commit()
    na_fila = db.scalars(select(Lead.nome).where(Lead.campanha_id == campanha.id).order_by(Lead.nome)).all()
    assert na_fila == ["Padaria A", "Padaria B"]
    # outra campanha não pega os mesmos leads
    outra = svc.salvar(db, None, nome="Outra", nicho="", oferta="", prompt_agente="")
    assert svc.adicionar_leads(db, outra, [busca.id]) == 0


def test_ativar_exige_mensagem_e_numero(db, campanha, conectado):
    with pytest.raises(CampanhaError, match="mensagem"):
        svc.ativar(db, campanha)
    svc.adicionar_mensagem(db, campanha, "Oi {empresa}")
    with pytest.raises(CampanhaError, match="número"):
        svc.ativar(db, campanha)
    svc.definir_numeros(db, campanha, [conectado.id])
    svc.ativar(db, campanha)
    assert campanha.status == CampanhaStatus.ATIVA
    svc.pausar(db, campanha)
    svc.ativar(db, campanha)
    assert campanha.status == CampanhaStatus.ATIVA


def test_encerrar_devolve_quem_nao_recebeu(db, busca, campanha, conectado):
    svc.adicionar_leads(db, campanha, [busca.id])
    a = db.scalar(select(Lead).where(Lead.nome == "Padaria A"))
    a.status, a.numero_id = LeadStatus.ENVIADO, conectado.id
    db.commit()
    assert svc.encerrar(db, campanha) == 1
    db.commit()
    b = db.scalar(select(Lead).where(Lead.nome == "Padaria B"))
    assert (b.status, b.campanha_id) == (LeadStatus.NOVO, None)
    assert db.get(Lead, a.id).campanha_id == campanha.id  # quem já recebeu continua na campanha
    with pytest.raises(CampanhaError):
        svc.adicionar_leads(db, campanha, [busca.id])


def test_gerar_mensagens_ia_descarta_marcador_invalido(db, campanha, monkeypatch):
    recebido = {}

    def fake(**kwargs):
        recebido.update(kwargs)
        return ["Oi {empresa}, tudo bem?", "Olá {nome}!", "Bom dia, {empresa} de {cidade}"]

    monkeypatch.setattr(ia, "gerar_variacoes", fake)
    assert svc.gerar_mensagens_ia(db, campanha) == 2
    assert {m.origem for m in campanha.mensagens} == {"ia"}
    assert (recebido["nicho"], recebido["oferta"]) == ("padarias", "mais pedidos")


def test_metricas(db, busca, campanha, conectado):
    svc.adicionar_leads(db, campanha, [busca.id])
    a = db.scalar(select(Lead).where(Lead.nome == "Padaria A"))
    from datetime import datetime, timezone

    a.enviado_em = a.respondeu_em = datetime.now(timezone.utc)
    a.status = LeadStatus.RESPONDEU
    db.commit()
    m = svc.metricas(db, campanha)
    assert (m["na_fila"], m["enviados"], m["respostas"], m["taxa_resposta"]) == (1, 1, 1, 100.0)


# --- painel -----------------------------------------------------------------------


def test_fluxo_pelo_painel(logged_client, busca, conectado, monkeypatch):
    resp = logged_client.post("/campanhas", data={"nome": "Padarias SP", "nicho": "padarias", "oferta": "x", "prompt_agente": ""})
    cid = int(resp.headers["location"].rsplit("/", 1)[1])

    resp = logged_client.post(f"/campanhas/{cid}/ativar")
    assert "erro=" in resp.headers["location"]

    resp = logged_client.post(f"/campanhas/{cid}/mensagens", data={"texto": "Oi {nome}"})
    assert "erro=" in resp.headers["location"]
    logged_client.post(f"/campanhas/{cid}/mensagens", data={"texto": "Oi {empresa}, tudo bem?"})

    monkeypatch.setattr(ia, "gerar_variacoes", lambda **kw: ["Olá {empresa}!"])
    resp = logged_client.post(f"/campanhas/{cid}/mensagens/gerar")
    assert "ok=" in resp.headers["location"]

    logged_client.post(f"/campanhas/{cid}/numeros", data={"numero_id": [str(conectado.id)]})
    resp = logged_client.post(f"/campanhas/{cid}/leads", data={"busca_id": [str(busca.id)]})
    assert "2%20leads" in resp.headers["location"]

    resp = logged_client.post(f"/campanhas/{cid}/ativar")
    assert "erro=" not in resp.headers["location"]

    pagina = logged_client.get(f"/campanhas/{cid}")
    assert "Oi {empresa}, tudo bem?" in pagina.text and "Olá {empresa}!" in pagina.text and "(IA)" in pagina.text
    with SessionLocal() as s:
        assert s.get(Campanha, cid).status == CampanhaStatus.ATIVA
    assert "Padarias SP" in logged_client.get("/campanhas").text


def test_erro_da_ia_aparece(logged_client, campanha, monkeypatch):
    def falha(**kw):
        raise ia.IAError("ANTHROPIC_API_KEY não configurada.")

    monkeypatch.setattr(ia, "gerar_variacoes", falha)
    resp = logged_client.post(f"/campanhas/{campanha.id}/mensagens/gerar")
    assert "ANTHROPIC_API_KEY" in logged_client.get(resp.headers["location"]).text


def test_blacklist_pelo_painel(logged_client, busca):
    logged_client.post("/blacklist", data={"telefone": "(19) 91111-0001", "motivo": "pediu por telefone"})
    pagina = logged_client.get("/blacklist")
    assert "+5519911110001" in pagina.text and "pediu por telefone" in pagina.text
    with SessionLocal() as s:
        assert s.scalar(select(Lead.status).where(Lead.telefone == "+5519911110001")) == LeadStatus.OPT_OUT
    assert "erro=" in logged_client.post("/blacklist", data={"telefone": "123"}).headers["location"]


def test_telas_exigem_login(client):
    for path in ("/campanhas", "/campanhas/nova", "/campanhas/1", "/config", "/blacklist"):
        assert client.get(path).headers["location"] == "/login"
