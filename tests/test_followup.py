import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import CampanhaStatus, Envio, EnvioStatus, EnvioTipo, Lead, LeadStatus, Mensagem
from app.services import config_sistema, disparo, followup, mensagens, numeros
from app.services.campanhas import CampanhaError
from app.services import campanhas
from tests.test_disparo import SEGUNDA_9H, criar_campanha, criar_numero
from tests.test_opt_out import payload

DIAS = [2, 5, 10]


@pytest.fixture(autouse=True)
def sem_virada(monkeypatch):
    monkeypatch.setattr(numeros, "garantir_virada", lambda db, agora=None: False)


def lead_enviado(db, campanha, numero, enviado_em, telefone="+5519911110001", nome="Padaria Sol"):
    lead = Lead(nome=nome, telefone=telefone, status=LeadStatus.ENVIADO, campanha_id=campanha.id,
                numero_id=numero.id, enviado_em=enviado_em)
    db.add(lead)
    db.commit()
    return lead


def test_vencimento_dos_passos():
    lead = Lead(nome="x", telefone="+5519911110001", enviado_em=SEGUNDA_9H, followups_enviados=0)
    assert followup.vencimento(lead, DIAS) == SEGUNDA_9H + timedelta(days=2)
    # lead antigo: o 1º sai agora e o 2º só 3 dias depois dele, nunca dois de uma vez
    lead.followups_enviados, lead.ultimo_followup_em = 1, SEGUNDA_9H + timedelta(days=30)
    assert followup.vencimento(lead, DIAS) == SEGUNDA_9H + timedelta(days=33)
    lead.followups_enviados = 2
    assert followup.vencimento(lead, DIAS) == SEGUNDA_9H + timedelta(days=35)
    lead.followups_enviados = 3
    assert followup.vencimento(lead, DIAS) is None


def test_papel_e_textos_de_fabrica(db):
    assert [followup.papel_do_passo(i, 3) for i in (1, 2, 3)] == ["lembrete", "retomada", "despedida"]
    assert followup.papel_do_passo(1, 1) == "lembrete"
    textos = followup.textos_de_fabrica(db, 2, 3)
    assert all("{" not in t.replace("{empresa}", "") for t in textos)
    assert any("Bruna" in t and "Brafort10" in t and "até 50%" in t for t in textos)


def test_dias_do_followup_validados(db):
    config_sistema.salvar(db, {"followup_dias": " 3, 7 "})
    assert followup.regras(db) == (True, [3, 7])
    for invalido in ("5,2", "0,3", "a,b", "", "1,2,3,4,5,6"):
        with pytest.raises(config_sistema.ConfigError):
            config_sistema.salvar(db, {"followup_dias": invalido})


def test_ciclo_manda_followup_antes_de_lead_novo(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = lead_enviado(db, c, n, SEGUNDA_9H - timedelta(days=2, minutes=1))
    db.add(Lead(nome="Nova", telefone="+5519911110009", status=LeadStatus.NA_FILA, campanha_id=c.id))
    db.commit()

    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 1
    envio = db.scalar(select(Envio))
    assert (envio.tipo, envio.lead_id, envio.numero_id, envio.status) == (EnvioTipo.FOLLOW_UP, lead.id, n.id, EnvioStatus.ENVIADO)
    assert envio.texto in [t.replace("{empresa}", "Padaria Sol") for t in followup.PADROES["lembrete"]]
    db.refresh(lead)
    db.refresh(n)
    assert (lead.followups_enviados, lead.status, n.envios_hoje) == (1, LeadStatus.ENVIADO, 1)
    assert db.scalar(select(Mensagem).where(Mensagem.lead_id == lead.id)).texto == envio.texto
    (_, instance, numero_destino, _), = evo.called("send_text")
    assert (instance, numero_destino) == (n.instance_name, "5519911110001")


def test_ainda_nao_venceu_vai_lead_novo(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead_enviado(db, c, n, SEGUNDA_9H - timedelta(days=1))
    db.add(Lead(nome="Nova", telefone="+5519911110009", status=LeadStatus.NA_FILA, campanha_id=c.id))
    db.commit()
    disparo.ciclo(db, SEGUNDA_9H, random.Random(1))
    assert db.scalar(select(Envio.tipo)) == EnvioTipo.ABERTURA


def test_texto_proprio_da_campanha(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    campanhas.adicionar_followup(db, c, 1, "E aí, {empresa}, viu minha mensagem?")
    with pytest.raises(CampanhaError):
        campanhas.adicionar_followup(db, c, 1, "Oi {nome}")
    lead_enviado(db, c, n, SEGUNDA_9H - timedelta(days=3))
    disparo.ciclo(db, SEGUNDA_9H, random.Random(1))
    assert db.scalar(select(Envio.texto)) == "E aí, Padaria Sol, viu minha mensagem?"


@pytest.mark.parametrize("cenario", ["respondeu", "campanha_pausada", "ia_desligada", "followup_desligado"])
def test_quem_nao_recebe_followup(db, server, evo, cenario):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = lead_enviado(db, c, n, SEGUNDA_9H - timedelta(days=3))
    if cenario == "respondeu":
        lead.status = LeadStatus.EM_CONVERSA
    elif cenario == "campanha_pausada":
        c.status = CampanhaStatus.PAUSADA
    elif cenario == "ia_desligada":
        lead.ia_ativa = False
    else:
        config_sistema.salvar(db, {"followup_ativo": ""})
    db.commit()
    disparo.ciclo(db, SEGUNDA_9H, random.Random(1))
    assert db.scalar(select(Envio).where(Envio.tipo == EnvioTipo.FOLLOW_UP)) is None


def test_tres_passos_e_depois_sem_resposta(db, server, evo):
    n = criar_numero(db, server, "Chip", 100)
    c = criar_campanha(db, [n])
    lead = lead_enviado(db, c, n, SEGUNDA_9H)
    agora = SEGUNDA_9H
    for dia in (2, 7, 14):  # quarta; segunda (o 2º venceu no sábado); segunda (o 3º venceu no sábado)
        agora = SEGUNDA_9H + timedelta(days=dia, minutes=1)
        n.proximo_envio_em = None
        n.envios_hoje = 0
        db.commit()
        assert disparo.ciclo(db, agora, random.Random(dia)) == 1
    textos = db.scalars(select(Envio.texto).where(Envio.tipo == EnvioTipo.FOLLOW_UP).order_by(Envio.id)).all()
    assert len(textos) == 3 and textos[2] in followup.textos_de_fabrica(db, 3, 3)

    n.proximo_envio_em = None
    db.commit()
    assert disparo.ciclo(db, agora + timedelta(days=1), random.Random(1)) == 0  # acabaram os passos
    db.refresh(lead)
    assert lead.status == LeadStatus.ENVIADO
    disparo.ciclo(db, agora + timedelta(days=3, minutes=1), random.Random(1))
    db.refresh(lead)
    assert lead.status == LeadStatus.SEM_RESPOSTA


def test_resposta_depois_do_ultimo_followup_volta_para_a_ia(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = lead_enviado(db, c, n, SEGUNDA_9H)
    lead.status = LeadStatus.SEM_RESPOSTA
    db.commit()
    assert mensagens.registrar_recebida(db, payload("oi, desculpa a demora")) == lead
    assert lead.status == LeadStatus.RESPONDEU and lead.responder_em is not None


def test_falha_no_envio_nao_fica_repetindo(db, server, evo, monkeypatch):
    from app.integrations.evolution import EvolutionError

    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = lead_enviado(db, c, n, SEGUNDA_9H - timedelta(days=3))
    evo.fail["send_text"] = EvolutionError(None, "caiu")
    disparo.ciclo(db, SEGUNDA_9H, random.Random(1))
    db.refresh(lead)
    assert lead.followups_enviados == 1
    assert db.scalar(select(Envio.status)) == EnvioStatus.FALHOU


def test_card_de_followup_na_campanha(logged_client, db, server):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    pagina = logged_client.get(f"/campanhas/{c.id}").text
    assert "Follow-up automático" in pagina and "1º follow-up · dia 2" in pagina and "3º follow-up · dia 10" in pagina
    assert "Usando os textos de fábrica" in pagina

    resp = logged_client.post(f"/campanhas/{c.id}/followups", data={"passo": "2", "texto": "Oi {empresa}, retomando!"})
    assert "erro=" not in resp.headers["location"]
    pagina = logged_client.get(f"/campanhas/{c.id}").text
    assert "Oi {empresa}, retomando!" in pagina
    from app.db import SessionLocal
    from app.models import CampanhaFollowup

    with SessionLocal() as s:
        item_id = s.scalar(select(CampanhaFollowup.id))
    logged_client.post(f"/campanhas/{c.id}/followups/{item_id}/remover")
    assert "Oi {empresa}, retomando!" not in logged_client.get(f"/campanhas/{c.id}").text
    assert "Dias do follow-up" in logged_client.get("/config").text
