import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Autor, Blacklist, Lead, LeadStatus, Mensagem
from app.services import campanhas, lead_teste
from app.services.lead_teste import LeadTesteError


@pytest.fixture
def campanha(db, conectado):
    c = campanhas.salvar(db, None, nome="Brafort pousadas", nicho="pousadas", oferta="", prompt_agente="")
    campanhas.adicionar_mensagem(db, c, "Olá! Sou a Bruna, da Brafort10. Essa é a {empresa}?")
    db.commit()
    return c


def test_cria_lead_e_manda_abertura_na_hora(db, conectado, campanha, evo):
    lead = lead_teste.criar(db, nome="Pousada Teste", telefone="(11) 98888-7777", numero=conectado, campanha=campanha,
                            categoria="Pousada", segmento="", porte="grande")
    db.commit()
    (_, instance, number, texto), = evo.called("send_text")
    assert (instance, number, texto) == (conectado.instance_name, "5511988887777", "Olá! Sou a Bruna, da Brafort10. Essa é a Pousada Teste?")
    assert (lead.status, lead.numero_id, lead.porte, lead.ia_ativa) == (LeadStatus.ENVIADO, conectado.id, "grande", True)
    assert db.scalar(select(Mensagem.autor).where(Mensagem.lead_id == lead.id)) == Autor.SISTEMA


def test_criar_de_novo_recomeca_do_zero(db, conectado, campanha, evo):
    lead = lead_teste.criar(db, nome="A", telefone="11988887777", numero=conectado, campanha=campanha)
    lead.status, lead.ia_ativa, lead.etapa = LeadStatus.HUMANO_ASSUMIU, False, "desejo"
    db.add(Mensagem(lead_id=lead.id, direcao="entrada", autor="lead", texto="oi"))
    db.commit()

    de_novo = lead_teste.criar(db, nome="B", telefone="11988887777", numero=conectado, campanha=campanha)
    db.commit()
    assert de_novo.id == lead.id
    assert (de_novo.nome, de_novo.status, de_novo.ia_ativa, de_novo.etapa) == ("B", LeadStatus.ENVIADO, True, None)
    assert db.scalar(select(func.count()).select_from(Mensagem).where(Mensagem.lead_id == lead.id)) == 1  # só a abertura nova


@pytest.mark.parametrize("telefone, msg", [("123", "Telefone inválido"), ("+5511999990001", "diferente do chip")])
def test_validacoes(db, conectado, campanha, telefone, msg):
    conectado.telefone = "+5511999990001"
    with pytest.raises(LeadTesteError, match=msg):
        lead_teste.criar(db, nome="x", telefone=telefone, numero=conectado, campanha=campanha)


def test_telefone_sem_whatsapp(db, conectado, campanha, evo):
    evo.sem_whatsapp = {"5511988887777"}
    with pytest.raises(LeadTesteError, match="não tem WhatsApp"):
        lead_teste.criar(db, nome="x", telefone="11988887777", numero=conectado, campanha=campanha)


def test_guarda_o_identificador_do_whatsapp(db, conectado, campanha, evo):
    lead = lead_teste.criar(db, nome="x", telefone="11988887777", numero=conectado, campanha=campanha)
    assert lead.whatsapp_jid == "5511988887777@s.whatsapp.net"


def test_telefone_na_blacklist(db, conectado, campanha):
    db.add(Blacklist(telefone="+5511988887777", motivo="opt-out"))
    db.commit()
    with pytest.raises(LeadTesteError, match="blacklist"):
        lead_teste.criar(db, nome="x", telefone="11988887777", numero=conectado, campanha=campanha)


def test_campanha_sem_abertura(db, conectado):
    c = campanhas.salvar(db, None, nome="Vazia", nicho="", oferta="", prompt_agente="")
    with pytest.raises(LeadTesteError, match="abertura"):
        lead_teste.criar(db, nome="x", telefone="11988887777", numero=conectado, campanha=c)


def test_pelo_painel(logged_client, conectado, campanha, evo):
    pagina = logged_client.get("/leads")
    assert "Criar lead de teste" in pagina.text
    resp = logged_client.post("/leads/teste", data={
        "telefone": "(11) 98888-7777", "nome": "Pousada Teste", "categoria": "Pousada", "cidade": "Angra",
        "segmento": "", "porte": "", "numero_id": conectado.id, "campanha_id": campanha.id,
    })
    assert resp.headers["location"].startswith("/leads/")
    assert "Pousada Teste" in logged_client.get(resp.headers["location"]).text
    with SessionLocal() as s:
        assert s.scalar(select(Lead.status)) == LeadStatus.ENVIADO

    resp = logged_client.post("/leads/teste", data={"telefone": "123", "numero_id": conectado.id, "campanha_id": campanha.id})
    assert "Telefone%20inv" in resp.headers["location"]
