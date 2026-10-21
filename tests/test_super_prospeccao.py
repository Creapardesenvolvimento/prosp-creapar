import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.integrations.evolution import EvolutionError
from app.models import Envio, EnvioStatus, EnvioTipo, Lead, LeadStatus, NumeroStatus
from app.services import alertas, campanhas, config_sistema, disparo, numeros, prospeccao
from app.services.agenda import utc
from app.services.campanhas import CampanhaError
from tests.test_disparo import SEGUNDA_9H, criar_campanha, criar_leads, criar_numero

NOTIFICACOES = "+5511988887777"


@pytest.fixture(autouse=True)
def sem_virada(monkeypatch, request):
    if "virada" not in request.node.name:
        monkeypatch.setattr(numeros, "garantir_virada", lambda db, agora=None: False)


def aberturas(db):
    return db.scalar(
        select(func.count()).select_from(Envio).where(Envio.tipo == EnvioTipo.ABERTURA, Envio.status == EnvioStatus.ENVIADO)
    )


def registrar_envios(db, numero, lead, quantidade, quando, tipo=EnvioTipo.ABERTURA):
    for i in range(quantidade):
        db.add(Envio(lead_id=lead.id, numero_id=numero.id, tipo=tipo, texto="x", status=EnvioStatus.ENVIADO,
                     enviado_em=utc(quando - timedelta(minutes=i))))  # o SQLite descarta o fuso
    db.commit()


# --- variações de texto ------------------------------------------------------------


def test_variacoes_no_texto():
    texto = "{Oi|Olá|Bom dia}, {empresa}! {Tudo bem?|}"
    assert campanhas.validar_texto(texto) == texto
    with pytest.raises(CampanhaError, match="Marcador desconhecido"):
        campanhas.validar_texto("{Oi|Olá} {nome}")
    assert campanhas.contar_combinacoes(texto) == 6
    lead = Lead(nome="Padaria Sol", telefone="+5519911110001", cidade="Campinas")
    saidas = {campanhas.renderizar(texto, lead, random.Random(i)) for i in range(60)}
    assert saidas == {f"{s}, Padaria Sol! {f}".strip() for s in ("Oi", "Olá", "Bom dia") for f in ("Tudo bem?", "")}
    assert campanhas.renderizar("Oi {empresa} de {cidade}", Lead(nome="", telefone="+5519911110001")) == "Oi sua empresa de sua cidade"


def test_disparo_sorteia_as_variacoes(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n], mensagens=("{Oi|Olá} {empresa}",))
    criar_leads(db, c, 1)
    disparo.ciclo(db, SEGUNDA_9H, random.Random(3))
    assert db.scalar(select(Envio.texto)) in ("Oi Padaria 0", "Olá Padaria 0")


# --- teto, meta e ritmo ------------------------------------------------------------------


def test_teto_por_numero_limita_a_rampa(db):
    config_sistema.salvar(db, {"rampa": "1:10,8:40,22:150", "teto_por_numero": "30"})
    assert config_sistema.rampa(db) == [(1, 10), (8, 30), (22, 30)]


def test_meta_do_dia_reparte_entre_os_numeros(db, server, evo):
    nums = [criar_numero(db, server, f"Chip {i}", 50) for i in range(3)]
    c = criar_campanha(db, nums)
    criar_leads(db, c, 20)
    config_sistema.salvar(db, {"meta_diaria": "2"})  # 2 no dia, em 9h de disparo: no máximo 1 por hora
    db.commit()
    for minutos, esperado in ((0, 1), (30, 0), (65, 1), (200, 0)):
        for n in nums:
            n.proximo_envio_em = None
        db.commit()
        assert disparo.ciclo(db, SEGUNDA_9H + timedelta(minutes=minutos), random.Random(minutos)) == esperado
    assert aberturas(db) == 2


def test_meta_batida_ainda_manda_followup(db, server, evo):
    n = criar_numero(db, server, "Chip", 50)
    c = criar_campanha(db, [n])
    antigo = Lead(nome="Antigo", telefone="+5519911110001", status=LeadStatus.ENVIADO, campanha_id=c.id,
                  numero_id=n.id, enviado_em=SEGUNDA_9H - timedelta(days=3))
    db.add(antigo)
    criar_leads(db, c, 5)
    config_sistema.salvar(db, {"meta_diaria": "1"})
    registrar_envios(db, n, antigo, 1, SEGUNDA_9H - timedelta(minutes=5))  # a meta de hoje já saiu
    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 1
    assert db.scalar(select(Envio.tipo).order_by(Envio.id.desc())) == EnvioTipo.FOLLOW_UP


def test_meta_por_hora_segura_rajada(db, server, evo):
    n = criar_numero(db, server, "Chip", 500)
    c = criar_campanha(db, [n])
    lead, *_ = criar_leads(db, c, 3)
    config_sistema.salvar(db, {"meta_diaria": "90", "limite_hora_numero": "60"})  # 9h de disparo -> 12 por hora
    assert disparo.meta_por_hora(config_sistema.regras_disparo(db), 90) == 12
    registrar_envios(db, n, lead, 12, SEGUNDA_9H + timedelta(minutes=50))
    assert disparo.ciclo(db, SEGUNDA_9H + timedelta(minutes=55), random.Random(1)) == 0


def test_maximo_por_hora_do_numero(db, server, evo):
    a = criar_numero(db, server, "Chip A", 50)
    b = criar_numero(db, server, "Chip B", 50)
    c = criar_campanha(db, [a, b])
    lead, *_ = criar_leads(db, c, 5)
    config_sistema.salvar(db, {"limite_hora_numero": "2"})
    registrar_envios(db, a, lead, 2, SEGUNDA_9H - timedelta(minutes=10))
    db.commit()
    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 1
    assert db.scalar(select(Envio.numero_id).order_by(Envio.id.desc())) == b.id


def test_cota_da_meta_espaca_os_envios(db, server):
    n = SimpleNamespace(limite_diario_atual=50, envios_hoje=0)
    regras = config_sistema.regras_disparo(db)
    sem_cota = disparo.proximo_intervalo(regras, n, SEGUNDA_9H, random.Random(1))
    com_cota = disparo.proximo_intervalo(regras, n, SEGUNDA_9H, random.Random(1), cota=5)
    assert com_cota > 4 * sem_cota  # 5 envios no dia ficam bem mais espaçados que 50


# --- proteção e avisos ----------------------------------------------------------


def test_falhas_seguidas_pausam_o_numero(db, server, evo):
    n = criar_numero(db, server, "Chip", 50)
    c = criar_campanha(db, [n])
    criar_leads(db, c, 5)
    evo.fail["send_text"] = EvolutionError(500, "caiu")
    agora = SEGUNDA_9H
    for _ in range(3):
        disparo.ciclo(db, agora, random.Random(1))
        agora += timedelta(minutes=10)
    db.refresh(n)
    assert (n.status, n.motivo_pausa) == (NumeroStatus.PAUSADO, "3 falhas seguidas no envio")


def test_aviso_de_numero_fora_do_rodizio(db, server, evo):
    a = criar_numero(db, server, "Chip A", 50)
    criar_numero(db, server, "Chip B", 50)
    assert not alertas.avisar_chip(db, a, "pausado", "teste")  # sem WhatsApp de notificações
    config_sistema.salvar(db, {"notificar_telefone": NOTIFICACOES})
    db.commit()
    numeros.aplicar_estado(db, a, "close", 401)
    numeros.aplicar_estado(db, a, "open")
    numeros.aplicar_estado(db, a, "close", 401)  # caiu de novo no mesmo dia: não repete o aviso
    avisos = [c for c in evo.called("send_text") if c[2] == NOTIFICACOES.lstrip("+")]
    assert len(avisos) == 1 and "Chip A" in avisos[0][3] and "desconectou" in avisos[0][3]

    config_sistema.salvar(db, {"alertar_chips": ""})
    assert not alertas.avisar_chip(db, a, "pausado", "outro motivo")


def test_virada_conta_followups_no_dia(db, server, evo):
    n = criar_numero(db, server, "Chip", 50)
    lead = Lead(nome="x", telefone="+5519911110001")
    db.add(lead)
    db.commit()
    registrar_envios(db, n, lead, 2, SEGUNDA_9H, EnvioTipo.FOLLOW_UP)
    registrar_envios(db, n, lead, 1, SEGUNDA_9H)
    numeros.virada_do_dia(db, SEGUNDA_9H + timedelta(hours=1))
    assert n.envios_hoje == 3


# --- painel ------------------------------------------------------------------------


def test_projecao_pela_rampa():
    agora = SEGUNDA_9H
    chips = [SimpleNamespace(data_ativacao=agora) for _ in range(25)]
    rampa = [(1, 10), (4, 20), (8, 40), (15, 50)]
    assert prospeccao.capacidade_no_dia(chips, rampa, agora, 0) == 250
    assert prospeccao.dia_em_que_alcanca(chips, rampa, agora, 1000) == 7
    assert prospeccao.dia_em_que_alcanca(chips, rampa, agora, 2000) is None


def test_painel_da_super_prospeccao(db, server, evo):
    nums = [criar_numero(db, server, f"Chip {i}", 10) for i in range(2)]
    c = criar_campanha(db, nums)
    criar_leads(db, c, 30)
    config_sistema.salvar(db, {"meta_diaria": "1000", "teto_por_numero": "50"})
    db.commit()
    p = prospeccao.painel(db, SEGUNDA_9H)
    assert (p["capacidade"]["necessarios"], p["capacidade"]["com_reserva"], p["capacidade"]["faltam"]) == (20, 24, 22)
    assert p["capacidade"]["conectados"] == 2 and p["na_fila"] == 30 and p["meta_hora"] == 128
    assert p["carga_followup"] == 2700  # 1000 × 90% sem resposta × 3 passos
    assert any("Faltam 22 números" in a for a in p["avisos"])
    assert any("menos de 1,5 dia" in a for a in p["avisos"])


def test_tela_da_super_prospeccao(logged_client, db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    criar_campanha(db, [n])
    pagina = logged_client.get("/prospeccao").text
    assert "Super prospecção" in pagina and "Chip" in pagina and "Defina a meta" in pagina

    resp = logged_client.post("/prospeccao/metas", data={"meta_diaria": "1000", "teto_por_numero": "8", "limite_hora_numero": "8"})
    assert "ok=" in resp.headers["location"]
    from app.db import SessionLocal
    from app.models import WhatsappNumber

    with SessionLocal() as s:
        assert s.get(WhatsappNumber, n.id).limite_diario_atual == 8  # o teto novo vale já hoje
    resp = logged_client.post("/prospeccao/metas", data={"meta_diaria": "-1", "teto_por_numero": "50", "limite_hora_numero": "8"})
    assert "erro=" in resp.headers["location"]
    assert "Super prospecção" in logged_client.get("/").text
