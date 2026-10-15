import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.integrations.evolution import EvolutionError
from app.models import (
    Blacklist,
    Campanha,
    CampanhaStatus,
    Envio,
    EnvioStatus,
    Lead,
    LeadStatus,
    NumeroStatus,
    WhatsappNumber,
)
from app.services import campanhas, config_sistema, disparo, numeros

SP = ZoneInfo("America/Sao_Paulo")
SEGUNDA_9H = datetime(2026, 10, 12, 9, 0, tzinfo=SP)


@pytest.fixture(autouse=True)
def sem_virada(monkeypatch, request):
    """Os testes do disparo ajustam os limites na mão; a virada automática tem testes próprios."""
    if "virada" not in request.node.name:
        monkeypatch.setattr(numeros, "garantir_virada", lambda db, agora=None: False)


def criar_numero(db, server, nome, limite):
    numero, _ = numeros.criar_numero(db, nome, server)
    numeros.aplicar_estado(db, numero, "open")
    numero.limite_diario_atual = limite
    db.commit()
    return numero


def criar_campanha(db, nums, mensagens=("Oi {empresa}, tudo bem? Aqui em {cidade} vocês…",), status=CampanhaStatus.ATIVA):
    c = campanhas.salvar(db, None, nome="Teste", nicho="padarias", oferta="mais clientes", prompt_agente="")
    for texto in mensagens:
        campanhas.adicionar_mensagem(db, c, texto)
    campanhas.definir_numeros(db, c, [n.id for n in nums])
    c.status = status
    db.commit()
    return c


def criar_leads(db, campanha, quantidade, inicio=0):
    leads = [
        Lead(nome=f"Padaria {i}", telefone=f"+55199{i:08d}", cidade="Campinas", status=LeadStatus.NA_FILA, campanha_id=campanha.id)
        for i in range(inicio, inicio + quantidade)
    ]
    db.add_all(leads)
    db.commit()
    return leads


def envios_por_numero(db):
    return dict(
        db.execute(
            select(Envio.numero_id, func.count()).where(Envio.status == EnvioStatus.ENVIADO).group_by(Envio.numero_id)
        ).all()
    )


# --- horário e ritmo -----------------------------------------------------------


@pytest.mark.parametrize(
    "quando, dentro",
    [
        (datetime(2026, 10, 12, 9, 0, tzinfo=SP), True),  # segunda 9h
        (datetime(2026, 10, 12, 8, 59, tzinfo=SP), False),
        (datetime(2026, 10, 12, 17, 59, tzinfo=SP), True),
        (datetime(2026, 10, 12, 18, 0, tzinfo=SP), False),
        (datetime(2026, 10, 17, 10, 0, tzinfo=SP), False),  # sábado
        (datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc), True),  # 9h em SP
    ],
)
def test_horario_comercial(db, quando, dentro):
    assert disparo.dentro_do_horario(config_sistema.regras_disparo(db), quando) is dentro


def test_horario_vem_das_configuracoes(db):
    config_sistema.salvar(db, {"disparo_dias": ["5"], "disparo_inicio": "10:00", "disparo_fim": "12:00"})
    regras = config_sistema.regras_disparo(db)
    assert disparo.dentro_do_horario(regras, datetime(2026, 10, 17, 11, 0, tzinfo=SP))
    assert not disparo.dentro_do_horario(regras, datetime(2026, 10, 12, 11, 0, tzinfo=SP))


def test_intervalo_respeita_minimo_e_maximo(db, server):
    regras = config_sistema.regras_disparo(db)
    numero = criar_numero(db, server, "Chip", 1000)  # limite alto: o ritmo não estica o intervalo
    rng = random.Random(1)
    intervalos = [disparo.proximo_intervalo(regras, numero, SEGUNDA_9H, rng) for _ in range(200)]
    assert min(intervalos) >= 120 and max(intervalos) <= 360


def test_intervalo_estica_para_espalhar_o_limite(db, server):
    regras = config_sistema.regras_disparo(db)
    numero = criar_numero(db, server, "Chip", 10)
    # 9h de expediente / (10 + 1) ≈ 49 min entre envios (±20%)
    intervalo = disparo.proximo_intervalo(regras, numero, SEGUNDA_9H, random.Random(1))
    assert 0.8 * 2945 <= intervalo <= 1.2 * 2946


# --- envio ----------------------------------------------------------------------


def test_envia_personalizado_e_registra(db, server, evo):
    numero = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [numero])
    (lead,) = criar_leads(db, c, 1)

    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 1

    (_, instance, number, text), = evo.called("send_text")
    assert (instance, number) == (numero.instance_name, "5519900000000")
    assert text == "Oi Padaria 0, tudo bem? Aqui em Campinas vocês…"
    db.refresh(lead)
    assert (lead.status, lead.numero_id) == (LeadStatus.ENVIADO, numero.id)
    envio = db.scalar(select(Envio))
    assert envio.status == EnvioStatus.ENVIADO and envio.evolution_message_id
    db.refresh(numero)
    assert numero.envios_hoje == 1
    assert numero.proximo_envio_em is not None


def test_nao_envia_fora_do_horario_nem_com_campanha_pausada(db, server, evo):
    numero = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [numero], status=CampanhaStatus.PAUSADA)
    criar_leads(db, c, 3)
    assert disparo.ciclo(db, SEGUNDA_9H) == 0
    c.status = CampanhaStatus.ATIVA
    db.commit()
    assert disparo.ciclo(db, datetime(2026, 10, 12, 19, 0, tzinfo=SP)) == 0
    assert evo.called("send_text") == []


def test_numero_desconectado_ou_pausado_nao_envia(db, server, evo):
    a = criar_numero(db, server, "A", 10)
    b = criar_numero(db, server, "B", 10)
    c = criar_campanha(db, [a, b])
    criar_leads(db, c, 5)
    numeros.aplicar_estado(db, a, "close")
    numeros.pausar(db, b)
    db.commit()
    assert disparo.ciclo(db, SEGUNDA_9H) == 0


def test_respeita_o_limite_diario(db, server, evo):
    numero = criar_numero(db, server, "Chip", 2)
    c = criar_campanha(db, [numero])
    criar_leads(db, c, 5)
    agora = SEGUNDA_9H
    for _ in range(20):
        disparo.ciclo(db, agora, random.Random(1))
        agora += timedelta(hours=1)
        if agora.hour >= 18:
            break
    assert envios_por_numero(db) == {numero.id: 2}


def test_distribuicao_proporcional_ao_limite(db, server, evo):
    pequeno = criar_numero(db, server, "Novo", 10)
    grande = criar_numero(db, server, "Antigo", 40)
    c = criar_campanha(db, [pequeno, grande], mensagens=("Oi {empresa}!", "Olá, {empresa}", "Bom dia {empresa}"))
    criar_leads(db, c, 100)
    rng = random.Random(42)

    agora, metade = SEGUNDA_9H, None
    while agora < SEGUNDA_9H.replace(hour=18):
        disparo.ciclo(db, agora, rng)
        if metade is None and agora >= SEGUNDA_9H.replace(hour=13, minute=30):
            metade = envios_por_numero(db)
        agora += timedelta(seconds=30)

    total = envios_por_numero(db)
    assert total == {pequeno.id: 10, grande.id: 40}  # cada um usa todo o seu limite, sem passar
    # no meio do expediente cada um mandou cerca de metade do próprio limite
    assert 3 <= metade[pequeno.id] <= 7
    assert 14 <= metade[grande.id] <= 26
    # e nenhum lead recebeu duas mensagens de abertura
    por_lead = Counter(db.scalars(select(Envio.lead_id).where(Envio.status == EnvioStatus.ENVIADO)))
    assert len(por_lead) == 50 and set(por_lead.values()) == {1}


def test_lead_nunca_recebe_de_dois_numeros(db, server, evo):
    a = criar_numero(db, server, "A", 10)
    b = criar_numero(db, server, "B", 10)
    c = criar_campanha(db, [a, b])
    (lead,) = criar_leads(db, c, 1)
    disparo.ciclo(db, SEGUNDA_9H)
    assert len(evo.called("send_text")) == 1
    db.refresh(lead)
    assert lead.numero_id in (a.id, b.id)
    # o lead já está amarrado: reservar de novo não devolve ninguém
    assert disparo.reservar_lead(db, b) is None and disparo.reservar_lead(db, a) is None


def test_varia_a_mensagem_entre_envios_do_mesmo_numero(db, server, evo):
    numero = criar_numero(db, server, "Chip", 1000)
    c = criar_campanha(db, [numero], mensagens=("Oi {empresa}!", "Olá, {empresa}."))
    criar_leads(db, c, 6)
    agora = SEGUNDA_9H
    for _ in range(6):
        disparo.ciclo(db, agora, random.Random(7))
        agora += timedelta(minutes=10)
    usadas = list(db.scalars(select(Envio.mensagem_id).order_by(Envio.id)))
    assert len(usadas) == 6
    assert all(x != y for x, y in zip(usadas, usadas[1:]))


def test_lead_na_blacklist_nao_recebe(db, server, evo):
    numero = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [numero])
    (lead,) = criar_leads(db, c, 1)
    db.add(Blacklist(telefone=lead.telefone, motivo="opt-out"))
    db.commit()
    assert disparo.ciclo(db, SEGUNDA_9H) == 0
    assert evo.called("send_text") == []
    db.refresh(lead)
    assert lead.status == LeadStatus.OPT_OUT


def test_falha_devolve_o_lead_para_a_fila_e_desiste_apos_3(db, server, evo):
    numero = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [numero])
    (lead,) = criar_leads(db, c, 1)
    evo.fail["send_text"] = EvolutionError(500, "erro interno")

    agora = SEGUNDA_9H
    for _ in range(5):
        disparo.ciclo(db, agora)
        agora += timedelta(minutes=10)

    db.refresh(lead)
    assert (lead.status, lead.numero_id, lead.tentativas_envio) == (LeadStatus.NA_FILA, None, 3)
    assert db.scalar(select(func.count()).select_from(Envio).where(Envio.status == EnvioStatus.FALHOU)) == 3
    db.refresh(numero)
    assert numero.envios_hoje == 0


# --- pausa por taxa de resposta ---------------------------------------------------


def _historico(db, numero, campanha, total, responderam):
    antigo = SEGUNDA_9H - timedelta(days=1)
    for i, lead in enumerate(criar_leads(db, campanha, total, inicio=1000)):
        lead.status, lead.numero_id, lead.enviado_em = LeadStatus.ENVIADO, numero.id, antigo
        lead.respondeu_em = antigo if i < responderam else None
        db.add(Envio(lead_id=lead.id, campanha_id=campanha.id, numero_id=numero.id, texto="oi", status=EnvioStatus.ENVIADO, enviado_em=antigo))
    db.commit()


def test_pausa_numero_com_taxa_de_resposta_baixa(db, server, evo):
    numero = criar_numero(db, server, "Chip", 100)
    c = criar_campanha(db, [numero])
    _historico(db, numero, c, total=50, responderam=1)  # 2%
    criar_leads(db, c, 3)

    assert disparo.ciclo(db, SEGUNDA_9H) == 0
    db.refresh(numero)
    assert numero.status == NumeroStatus.PAUSADO
    assert "taxa de resposta baixa (2%" in numero.motivo_pausa
    assert numeros.numeros_pausados_automaticamente(db) == [numero]
    assert evo.called("send_text") == []


def test_nao_pausa_com_taxa_saudavel_ou_poucos_envios(db, server, evo):
    numero = criar_numero(db, server, "Chip", 100)
    c = criar_campanha(db, [numero])
    _historico(db, numero, c, total=50, responderam=5)  # 10%
    criar_leads(db, c, 1)
    assert disparo.ciclo(db, SEGUNDA_9H) == 1
    db.refresh(numero)
    assert numero.status == NumeroStatus.CONECTADO


def test_pausa_automatica_pode_ser_desligada(db, server, evo):
    config_sistema.salvar(db, {"pausa_taxa_minima": "0"})
    numero = criar_numero(db, server, "Chip", 100)
    c = criar_campanha(db, [numero])
    _historico(db, numero, c, total=50, responderam=0)
    criar_leads(db, c, 1)
    assert disparo.ciclo(db, SEGUNDA_9H) == 1


def test_retomar_limpa_o_motivo_da_pausa(db, server, evo):
    numero = criar_numero(db, server, "Chip", 100)
    numero.status, numero.motivo_pausa = NumeroStatus.PAUSADO, "taxa de resposta baixa"
    evo.state = "open"
    numeros.retomar(db, numero)
    assert (numero.status, numero.motivo_pausa) == (NumeroStatus.CONECTADO, None)


# --- virada do dia garantida pelo disparo -------------------------------------------


def test_virada_perdida_a_meia_noite_acontece_no_ciclo(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    n.data_ativacao = SEGUNDA_9H - timedelta(days=10)  # rampa: 40/dia
    n.envios_hoje = 10  # preso no limite de ontem
    c = criar_campanha(db, [n])
    db.add(Lead(nome="Padaria A", telefone="+5519911110001", status=LeadStatus.NA_FILA, campanha_id=c.id))
    db.commit()
    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 1
    db.refresh(n)
    assert (n.envios_hoje, n.limite_diario_atual) == (1, 40)


def test_virada_roda_uma_vez_e_conta_os_envios_de_hoje(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = Lead(nome="A", telefone="+5519911110001", status=LeadStatus.ENVIADO, campanha_id=c.id)
    db.add(lead)
    db.flush()
    db.add(Envio(lead_id=lead.id, campanha_id=c.id, numero_id=n.id, mensagem_id=c.mensagens[0].id, tipo="abertura",
                 texto="x", status=EnvioStatus.ENVIADO, enviado_em=SEGUNDA_9H - timedelta(minutes=30)))
    db.add(Envio(lead_id=lead.id, campanha_id=c.id, numero_id=n.id, mensagem_id=c.mensagens[0].id, tipo="abertura",
                 texto="x", status=EnvioStatus.ENVIADO, enviado_em=SEGUNDA_9H - timedelta(days=1)))  # ontem
    n.envios_hoje = 7
    db.commit()
    assert numeros.garantir_virada(db, SEGUNDA_9H) is True
    assert n.envios_hoje == 1  # só o envio de hoje conta: rodar no meio do dia não devolve cota
    n.envios_hoje = 5
    assert numeros.garantir_virada(db, SEGUNDA_9H + timedelta(hours=2)) is False
    assert n.envios_hoje == 5
    assert numeros.garantir_virada(db, SEGUNDA_9H + timedelta(days=1)) is True


def test_diagnostico_explica_por_que_parou(db, server):
    a = criar_numero(db, server, "Chip A", 10)
    b = criar_numero(db, server, "Chip B", 10)
    a.envios_hoje = 10
    b.status, b.motivo_pausa = NumeroStatus.PAUSADO, "taxa de resposta baixa"
    c = criar_campanha(db, [a, b])
    d = campanhas.diagnostico(db, c, SEGUNDA_9H)
    assert d["no_horario"] is True
    assert [x["ok"] for x in d["numeros"]] == [False, False]
    assert "limite de hoje atingido (10/10)" in d["numeros"][0]["texto"]
    assert "pausado (taxa de resposta baixa)" in d["numeros"][1]["texto"]
    assert campanhas.diagnostico(db, c, SEGUNDA_9H.replace(hour=20))["no_horario"] is False
