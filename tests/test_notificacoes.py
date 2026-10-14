from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Reuniao, ReuniaoStatus, WebhookEvent
from app.services import agenda as svc
from app.services import config_sistema
from app.services.agenda import AgendaError, Regras
from app.services.config_sistema import ConfigError
from app.tasks.agenda import avisar_cancelamento, avisar_vendedor, resumo_automatico

SP = ZoneInfo("America/Sao_Paulo")
REGRAS = Regras(duracao=timedelta(minutes=30), buffer=timedelta(minutes=15), antecedencia=timedelta(hours=2), dias_a_frente=7, tz=SP)
AGORA = datetime(2026, 10, 12, 8, 0, tzinfo=SP)  # segunda
SEGUNDA = date(2026, 10, 12)
NOTIFICACOES = "+5511955554444"


def sp(dia, hora, minuto=0):
    return datetime(2026, 10, dia, hora, minuto, tzinfo=SP)


@pytest.fixture
def ana(db):
    v = svc.salvar_vendedor(
        db, None, nome="Ana", telefone="(11) 97777-6666", email="", link_reuniao="https://meet.google.com/x",
        ativo=True, faixas_por_dia={0: "10:00-17:00", 1: "10:00-12:00"},
    )
    db.commit()
    return v


def configurar(db, **valores):
    config_sistema.salvar(db, valores)
    db.commit()


def reuniao(db, inicio, nome="Pousada Mar Azul", obs="Quer economizar no enxoval."):
    r = svc.agendar(db, inicio, lead_nome=nome, lead_telefone="(21) 98888-7777", observacoes=obs, agora=AGORA, regras=REGRAS)
    db.commit()
    return r


def destinos(evo):
    return [numero for (_, _, numero, _) in evo.called("send_text")]


# --- configurações ----------------------------------------------------------------


def test_telefone_de_notificacoes_e_opcional_e_validado(db, conectado):
    assert config_sistema.valores(db)["notificar_telefone"] == ""
    configurar(db, notificar_telefone="(11) 95555-4444")
    assert config_sistema.valores(db)["notificar_telefone"] == NOTIFICACOES
    configurar(db, notificar_telefone="")
    assert svc.telefone_notificacoes(db) is None
    with pytest.raises(ConfigError, match="inválido"):
        config_sistema.salvar(db, {"notificar_telefone": "123"})


def test_telefone_de_notificacoes_nao_pode_ser_chip_de_prospeccao(db, conectado):
    conectado.telefone = NOTIFICACOES
    db.commit()
    with pytest.raises(ConfigError, match="prospecção"):
        config_sistema.salvar(db, {"notificar_telefone": "(11) 95555-4444"})


# --- aviso de reunião nova ----------------------------------------------------------


def test_mensagem_no_formato_pedido(db, ana):
    texto = svc.mensagem_aviso(reuniao(db, sp(12, 14)))
    linhas = texto.splitlines()
    assert linhas[1:5] == [
        "Horário: segunda, 12/10 às 14:00",
        "Nome: Pousada Mar Azul",
        "Telefone: +5521988887777",
        "Observação: Quer economizar no enxoval.",
    ]
    assert "Vendedor: Ana" in texto and "Link: https://meet.google.com/x" in texto


def test_avisa_vendedor_e_notificacoes(db, ana, conectado, evo):
    configurar(db, notificar_telefone=NOTIFICACOES)
    r = reuniao(db, sp(12, 14))
    avisar_vendedor.delay(r.id)
    assert destinos(evo) == ["5511977776666", "5511955554444"]
    avisar_vendedor.delay(r.id)  # nova tentativa não repete
    assert len(evo.called("send_text")) == 2
    with SessionLocal() as s:
        salva = s.get(Reuniao, r.id)
        assert salva.vendedor_avisado_em and salva.notificado_em


def test_vendedor_desligado_so_notificacoes_recebe(db, ana, conectado, evo):
    configurar(db, notificar_telefone=NOTIFICACOES, avisar_vendedor="")
    r = reuniao(db, sp(12, 14))
    avisar_vendedor.delay(r.id)
    assert destinos(evo) == ["5511955554444"]

    with SessionLocal() as s:
        svc.mudar_status(s, s.get(Reuniao, r.id), ReuniaoStatus.CANCELADA)
        s.commit()
    avisar_cancelamento.delay(r.id)
    assert destinos(evo) == ["5511955554444", "5511955554444"]
    assert "Reunião cancelada" in evo.called("send_text")[-1][3]


def test_sem_telefone_de_notificacoes_fica_como_antes(db, ana, conectado, evo):
    r = reuniao(db, sp(12, 14))
    avisar_vendedor.delay(r.id)
    assert destinos(evo) == ["5511977776666"]


# --- resumo do dia ------------------------------------------------------------------


def test_resumo_lista_so_as_agendadas_do_dia_em_ordem(db, ana):
    tarde = reuniao(db, sp(12, 15), nome="Hotel B", obs="")
    cedo = reuniao(db, sp(12, 10), nome="Hotel A")
    cancelada = reuniao(db, sp(12, 11), nome="Hotel C")
    svc.mudar_status(db, cancelada, ReuniaoStatus.CANCELADA)
    reuniao(db, sp(13, 10), nome="Outro dia")
    db.commit()

    lista = svc.reunioes_do_dia(db, SEGUNDA, REGRAS)
    assert [r.id for r in lista] == [cedo.id, tarde.id]
    texto = svc.mensagem_resumo(SEGUNDA, lista, REGRAS)
    assert texto.startswith("📋 *Reuniões de segunda, 12/10* (2)")
    assert "*1) 10:00* · Hotel A" in texto and "*2) 15:00* · Hotel B" in texto
    assert "Observação: Quer economizar no enxoval." in texto and "Hotel C" not in texto


def test_enviar_resumo_exige_telefone_e_reunioes(db, ana, conectado, evo):
    with pytest.raises(AgendaError, match="Configurações"):
        svc.enviar_resumo(db, SEGUNDA, REGRAS)
    configurar(db, notificar_telefone=NOTIFICACOES)
    with pytest.raises(AgendaError, match="Nenhuma reunião"):
        svc.enviar_resumo(db, SEGUNDA, REGRAS)
    reuniao(db, sp(12, 10))
    assert svc.enviar_resumo(db, SEGUNDA, REGRAS) == 1
    assert destinos(evo) == ["5511955554444"]


def test_resumo_automatico_na_hora_e_uma_vez_por_dia(db, ana, conectado, evo):
    reuniao(db, sp(12, 14))
    configurar(db, notificar_telefone=NOTIFICACOES)
    assert svc.resumo_automatico(db, sp(12, 9)) is None  # desligado de fábrica
    configurar(db, resumo_automatico="1", resumo_hora="08:00")
    assert svc.resumo_automatico(db, sp(12, 7, 55)) is None  # ainda não é a hora
    assert svc.resumo_automatico(db, sp(12, 8, 1)) == 1
    db.commit()
    assert svc.resumo_automatico(db, sp(12, 8, 6)) is None  # o beat de novo não repete
    assert len(evo.called("send_text")) == 1
    assert "Reuniões de segunda, 12/10" in evo.called("send_text")[0][3]


def test_resumo_automatico_dia_sem_reuniao_nao_envia_depois(db, ana, conectado, evo):
    configurar(db, notificar_telefone=NOTIFICACOES, resumo_automatico="1", resumo_hora="08:00")
    assert svc.resumo_automatico(db, sp(13, 8, 1)) == 0
    db.commit()
    db.add(Reuniao(vendedor_id=ana.id, lead_nome="Tarde", inicio=sp(13, 15), fim=sp(13, 15, 30)))
    db.commit()
    assert svc.resumo_automatico(db, sp(13, 11)) is None  # reunião marcada depois da hora não dispara resumo
    assert evo.called("send_text") == []


def test_resumo_automatico_sem_numero_tenta_de_novo(db, ana, evo, monkeypatch):
    configurar(db, notificar_telefone=NOTIFICACOES, resumo_automatico="1", resumo_hora="00:00")
    db.add(Reuniao(vendedor_id=ana.id, lead_nome="Hoje", inicio=datetime.now(SP) + timedelta(minutes=1), fim=datetime.now(SP) + timedelta(minutes=31)))
    db.commit()
    assert resumo_automatico.delay().get() is None  # nenhum número conectado: nada fica marcado
    with SessionLocal() as s:
        assert s.scalar(select(WebhookEvent).where(WebhookEvent.origem == "resumo-do-dia")) is None


# --- telas ------------------------------------------------------------------------


def test_tela_reunioes_do_dia(logged_client, db, ana, conectado, evo):
    r = reuniao(db, sp(12, 14))
    pagina = logged_client.get("/agenda/dia?data=2026-10-12")
    assert "Reuniões de segunda, 12/10" in pagina.text and "Pousada Mar Azul" in pagina.text
    assert "Quer economizar no enxoval." in pagina.text and "Cadastre o WhatsApp de notificações" in pagina.text

    resp = logged_client.post("/agenda/dia/enviar", data={"data": "2026-10-12"})
    assert "erro=" in resp.headers["location"]

    configurar(db, notificar_telefone=NOTIFICACOES)
    resp = logged_client.post("/agenda/dia/enviar", data={"data": "2026-10-12"})
    assert "ok=" in resp.headers["location"]
    assert destinos(evo) == ["5511955554444"]

    resp = logged_client.post(f"/reunioes/{r.id}/observacoes", data={"observacoes": " Pediu catálogo. ", "voltar": "https://evil.com"})
    assert resp.headers["location"] == "/agenda"
    with SessionLocal() as s:
        assert s.get(Reuniao, r.id).observacoes == "Pediu catálogo."

    assert "Nenhuma reunião agendada" in logged_client.get("/agenda/dia?data=2026-10-14").text
    assert "Ver reuniões do dia" in logged_client.get("/").text
    assert "WhatsApp que recebe as notificações" in logged_client.get("/config").text


def test_config_salva_notificacoes_pelo_painel(logged_client):
    form = {"disparo_dias": ["0", "1"], "notificar_telefone": "(11) 95555-4444", "resumo_hora": "07:30", "resumo_automatico": "1"}
    resp = logged_client.post("/config", data=form)
    assert "ok=" in resp.headers["location"]
    with SessionLocal() as s:
        v = config_sistema.valores(s)
    assert (v["notificar_telefone"], v["resumo_hora"], v["resumo_automatico"], v["avisar_vendedor"]) == (
        NOTIFICACOES, "07:30", True, False,  # checkbox desmarcado no formulário = desligado
    )
