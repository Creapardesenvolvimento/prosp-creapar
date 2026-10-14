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


# --- barra de progresso -------------------------------------------------------------


def _leads_com_status(db, campanha, *status):
    for i, st in enumerate(status):
        db.add(Lead(nome=f"L{i}", telefone=f"+55199{i:08d}", status=st, campanha_id=campanha.id))
    db.commit()


def test_progresso_por_fase(db, campanha, conectado):
    campanhas_svc = svc
    campanhas_svc.definir_numeros(db, campanha, [conectado.id])
    conectado.limite_diario_atual, conectado.envios_hoje = 10, 4
    _leads_com_status(
        db, campanha,
        LeadStatus.NA_FILA, LeadStatus.NA_FILA, LeadStatus.NA_FILA, LeadStatus.ENVIADO, LeadStatus.EM_CONVERSA,
        LeadStatus.RESPONDEU, LeadStatus.REUNIAO_AGENDADA, LeadStatus.HUMANO_ASSUMIU, LeadStatus.OPT_OUT, LeadStatus.SEM_INTERESSE,
    )
    p = svc.progresso(db, campanha)
    assert (p["total"], p["contatados"], p["na_fila"], p["pct_contatados"]) == (10, 7, 3, 70)
    assert {f["chave"]: f["quantidade"] for f in p["fases"]} == {
        "reuniao": 1, "conversa": 2, "humano": 1, "aguardando": 1, "encerrado": 2, "fila": 3,
    }
    assert sum(f["pct"] for f in p["fases"]) == 100
    assert (p["por_dia"], p["restante_hoje"], p["dias_restantes"], p["concluida"]) == (10, 6, 0, False)  # 3 cabem hoje


def test_previsao_de_dias(db, campanha, conectado):
    svc.definir_numeros(db, campanha, [conectado.id])
    conectado.limite_diario_atual, conectado.envios_hoje = 10, 10
    _leads_com_status(db, campanha, *[LeadStatus.NA_FILA] * 25)
    assert svc.progresso(db, campanha)["dias_restantes"] == 3  # nada mais hoje; 10 + 10 + 5


def test_fila_concluida_e_campanha_vazia(db, campanha, conectado):
    assert svc.progresso(db, campanha)["vazia"] is True
    _leads_com_status(db, campanha, LeadStatus.ENVIADO, LeadStatus.EM_CONVERSA)
    p = svc.progresso(db, campanha)
    assert p["concluida"] is True and p["dias_restantes"] is None


def test_progresso_nas_telas(logged_client, db, campanha, conectado):
    svc.definir_numeros(db, campanha, [conectado.id])
    campanha.status = CampanhaStatus.ATIVA
    db.commit()
    assert "Nenhum lead nesta campanha ainda" in logged_client.get(f"/campanhas/{campanha.id}").text
    _leads_com_status(db, campanha, LeadStatus.NA_FILA, LeadStatus.ENVIADO)

    pagina = logged_client.get(f"/campanhas/{campanha.id}")
    assert "1 de 2 contatados (50%)" in pagina.text and f'hx-get="/campanhas/{campanha.id}/progresso"' in pagina.text
    parcial = logged_client.get(f"/campanhas/{campanha.id}/progresso")
    assert parcial.status_code == 200 and "Progresso da prospecção" in parcial.text
    assert "1/2" in logged_client.get("/campanhas").text
    assert "1 de 2 contatados" in logged_client.get("/").text  # painel inicial mostra as campanhas ativas

    with SessionLocal() as s:
        lead = s.scalar(select(Lead).where(Lead.status == LeadStatus.NA_FILA))
        lead.status = LeadStatus.ENVIADO
        s.commit()
    pagina = logged_client.get(f"/campanhas/{campanha.id}/progresso")
    assert "Fila concluída" in pagina.text and "Adicionar mais leads" in pagina.text
