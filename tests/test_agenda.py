from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ReuniaoStatus, Vendedor
from app.services import agenda as svc
from app.services.agenda import AgendaError, AgendaVendedor, HorarioIndisponivel, Regras, calcular_horarios

SP = ZoneInfo("America/Sao_Paulo")
REGRAS = Regras(
    duracao=timedelta(minutes=30),
    buffer=timedelta(minutes=15),
    antecedencia=timedelta(hours=2),
    dias_a_frente=7,
    tz=SP,
)
# segunda-feira, 12/10/2026, 08:00 em São Paulo
AGORA = datetime(2026, 10, 12, 8, 0, tzinfo=SP)


def sp(dia, hora, minuto=0):
    return datetime(2026, 10, dia, hora, minuto, tzinfo=SP)


def inicios(horarios):
    return [h.inicio for h in horarios]


def agenda(vid=1, faixas=((0, "10:00", "12:00"),), bloqueios=(), reunioes=()):
    return AgendaVendedor(
        vid,
        [(d, time.fromisoformat(i), time.fromisoformat(f)) for d, i, f in faixas],
        list(bloqueios),
        list(reunioes),
    )


# --- cálculo puro --------------------------------------------------------------


def test_faixa_vira_horarios_de_30_min():
    horarios = calcular_horarios([agenda()], AGORA, REGRAS)
    assert inicios(horarios)[:4] == [sp(12, 10), sp(12, 10, 30), sp(12, 11), sp(12, 11, 30)]
    assert horarios[0].fim == sp(12, 10, 30)


def test_reuniao_que_nao_cabe_no_fim_da_faixa_fica_de_fora():
    horarios = calcular_horarios([agenda(faixas=((0, "10:00", "10:50"),))], AGORA, REGRAS)
    assert [h for h in inicios(horarios) if h.date() == sp(12, 0).date()] == [sp(12, 10)]


def test_respeita_antecedencia_minima():
    horarios = calcular_horarios([agenda()], sp(12, 9, 10), REGRAS)  # mínimo: 11:10
    assert inicios(horarios)[0] == sp(12, 11, 30)


def test_so_dias_configurados_e_dentro_da_janela():
    horarios = calcular_horarios([agenda(faixas=((2, "09:00", "10:00"),))], AGORA, REGRAS)  # quartas
    assert inicios(horarios) == [sp(14, 9), sp(14, 9, 30)]  # a quarta seguinte (21/10) passa dos 7 dias
    assert all(h.weekday() == 2 for h in inicios(horarios))


def test_bloqueio_remove_horarios():
    bloqueio = (sp(12, 10, 15), sp(12, 11, 0))
    horarios = calcular_horarios([agenda(bloqueios=[bloqueio])], AGORA, REGRAS)
    dia = [h for h in inicios(horarios) if h.date() == sp(12, 0).date()]
    assert dia == [sp(12, 11), sp(12, 11, 30)]


def test_buffer_entre_reunioes():
    reuniao = (sp(12, 10, 30), sp(12, 11, 0))
    horarios = calcular_horarios([agenda(reunioes=[reuniao])], AGORA, REGRAS)
    dia = [h for h in inicios(horarios) if h.date() == sp(12, 0).date()]
    # 10:00 termina 10:30 (sem os 15 min de intervalo); 11:00 começa colado na reunião
    assert dia == [sp(12, 11, 30)]


def test_varios_vendedores_somam_horarios_e_capacidade():
    a = agenda(1, faixas=((0, "10:00", "11:00"),))
    b = agenda(2, faixas=((0, "10:30", "11:30"),))
    horarios = [h for h in calcular_horarios([a, b], AGORA, REGRAS) if h.inicio.date() == sp(12, 0).date()]
    assert [(h.inicio, h.vendedores) for h in horarios] == [
        (sp(12, 10), (1,)),
        (sp(12, 10, 30), (1, 2)),
        (sp(12, 11), (2,)),
    ]


def test_sem_vendedores_sem_horarios():
    assert calcular_horarios([], AGORA, REGRAS) == []


def test_sugere_opcoes_espalhadas():
    horarios = calcular_horarios([agenda(faixas=((0, "10:00", "12:00"), (1, "10:00", "12:00")))], AGORA, REGRAS)
    assert inicios(svc.sugerir_horarios(horarios)) == [sp(12, 10), sp(13, 10)]


def test_sugere_mesmo_dia_quando_ha_3h_de_distancia():
    horarios = calcular_horarios([agenda(faixas=((0, "10:00", "16:00"),))], AGORA, REGRAS)
    assert inicios(svc.sugerir_horarios(horarios))[:2] == [sp(12, 10), sp(12, 13)]


def test_sugere_o_que_tiver_quando_ha_poucos_horarios():
    horarios = calcular_horarios([agenda(faixas=((0, "10:00", "11:00"),))], sp(13, 8), REGRAS)
    # só segunda que vem: 10:00 e 10:30
    assert inicios(svc.sugerir_horarios(horarios)) == [sp(19, 10), sp(19, 10, 30)]


# --- com banco ------------------------------------------------------------------


@pytest.fixture
def vendedores(db):
    def criar(nome, faixas, link=None, telefone=""):
        v = svc.salvar_vendedor(
            db, None, nome=nome, telefone=telefone, email="", link_reuniao=link or "", ativo=True, faixas_por_dia=faixas
        )
        db.commit()
        return v

    return criar


def test_agendar_reserva_e_tira_o_horario(db, vendedores):
    ana = vendedores("Ana", {0: "10:00-12:00"}, link="https://meet.google.com/abc-defg-hij")
    reuniao = svc.agendar(db, sp(12, 10), lead_nome="Padaria X", lead_telefone="(11) 98888-7777", agora=AGORA, regras=REGRAS)

    assert reuniao.vendedor_id == ana.id
    assert reuniao.status == ReuniaoStatus.AGENDADA
    assert reuniao.link == "https://meet.google.com/abc-defg-hij"
    assert reuniao.lead_telefone == "+5511988887777"
    livres = [h for h in inicios(svc.horarios_livres(db, AGORA, REGRAS)) if h.date() == sp(12, 0).date()]
    assert livres == [sp(12, 11), sp(12, 11, 30)]  # 10:00 ocupado, 10:30 cai no intervalo


def test_horario_ocupado_nao_pode_ser_reservado_de_novo(db, vendedores):
    vendedores("Ana", {0: "10:00-12:00"})
    svc.agendar(db, sp(12, 10), lead_nome="A", agora=AGORA, regras=REGRAS)
    with pytest.raises(HorarioIndisponivel):
        svc.agendar(db, sp(12, 10), lead_nome="B", agora=AGORA, regras=REGRAS)
    with pytest.raises(HorarioIndisponivel):
        svc.agendar(db, sp(12, 10, 30), lead_nome="C", agora=AGORA, regras=REGRAS)  # dentro do intervalo


def test_horario_fora_da_grade_e_recusado(db, vendedores):
    vendedores("Ana", {0: "10:00-12:00"})
    with pytest.raises(HorarioIndisponivel):
        svc.agendar(db, sp(12, 10, 10), lead_nome="A", agora=AGORA, regras=REGRAS)


def test_reuniao_vai_para_quem_tem_menos_na_semana(db, vendedores):
    ana = vendedores("Ana", {0: "10:00-12:00", 1: "10:00-12:00"})
    bia = vendedores("Bia", {0: "10:00-12:00", 1: "10:00-12:00"})

    primeira = svc.agendar(db, sp(12, 10), lead_nome="A", agora=AGORA, regras=REGRAS)
    segunda = svc.agendar(db, sp(12, 10), lead_nome="B", agora=AGORA, regras=REGRAS)  # mesmo horário, outro vendedor
    terceira = svc.agendar(db, sp(13, 11), lead_nome="C", agora=AGORA, regras=REGRAS)

    assert primeira.vendedor_id == ana.id  # empate: menor id
    assert segunda.vendedor_id == bia.id
    assert terceira.vendedor_id == ana.id  # empate de novo (1 x 1)
    quarta = svc.agendar(db, sp(13, 10), lead_nome="D", agora=AGORA, regras=REGRAS)
    assert quarta.vendedor_id == bia.id  # Ana tem 2, Bia 1
    with pytest.raises(HorarioIndisponivel):
        svc.agendar(db, sp(12, 10), lead_nome="E", agora=AGORA, regras=REGRAS)


def test_cancelar_libera_o_horario(db, vendedores):
    vendedores("Ana", {0: "10:00-12:00"})
    reuniao = svc.agendar(db, sp(12, 10), lead_nome="A", agora=AGORA, regras=REGRAS)
    svc.mudar_status(db, reuniao, ReuniaoStatus.CANCELADA)
    assert sp(12, 10) in inicios(svc.horarios_livres(db, AGORA, REGRAS))
    with pytest.raises(AgendaError):
        svc.mudar_status(db, reuniao, ReuniaoStatus.REALIZADA)


def test_vendedor_inativo_sai_da_agenda(db, vendedores):
    ana = vendedores("Ana", {0: "10:00-12:00"})
    ana.ativo = False
    db.commit()
    assert svc.horarios_livres(db, AGORA, REGRAS) == []


def test_bloqueio_no_banco(db, vendedores):
    ana = vendedores("Ana", {0: "10:00-12:00"})
    svc.adicionar_bloqueio(db, ana, "2026-10-12T00:00", "2026-10-12T23:59", "folga", REGRAS)
    db.commit()
    assert all(h.date() != sp(12, 0).date() for h in inicios(svc.horarios_livres(db, AGORA, REGRAS)))
    with pytest.raises(AgendaError):
        svc.adicionar_bloqueio(db, ana, "2026-10-13T10:00", "2026-10-13T09:00", "", REGRAS)


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("", []),
        ("09:00-12:00", [(time(9), time(12))]),
        ("14:00-18:00, 9:00-12:00", [(time(9), time(12)), (time(14), time(18))]),
    ],
)
def test_parse_faixas(texto, esperado):
    assert svc.parse_faixas(texto) == esperado


@pytest.mark.parametrize("texto", ["9-12", "12:00-09:00", "09:00-12:00, 11:00-13:00", "25:00-26:00"])
def test_parse_faixas_invalidas(texto):
    with pytest.raises(AgendaError):
        svc.parse_faixas(texto)


def test_salvar_vendedor_valida_telefone(db):
    with pytest.raises(AgendaError):
        svc.salvar_vendedor(db, None, nome="Ana", telefone="123", email="", link_reuniao="", ativo=True, faixas_por_dia={})


def test_avisar_vendedor_por_numero_conectado(db, server, vendedores, evo):
    from app.services import numeros

    numero, _ = numeros.criar_numero(db, "Chip 1", server)
    numeros.aplicar_estado(db, numero, "open")
    db.commit()
    vendedores("Ana", {0: "10:00-12:00"}, link="https://meet.google.com/x", telefone="(11) 97777-6666")
    reuniao = svc.agendar(db, sp(12, 10), lead_nome="Padaria X", agora=AGORA, regras=REGRAS)

    assert svc.avisar_vendedor(db, reuniao) is not None
    (_, instance, number, text), = evo.called("send_text")
    assert (instance, number) == (numero.instance_name, "5511977776666")
    assert "Padaria X" in text and "segunda, 12/10 às 10:00" in text and "https://meet.google.com/x" in text
    assert svc.avisar_vendedor(db, reuniao) is None  # não avisa duas vezes


def test_avisar_vendedor_sem_numero_conectado(db, vendedores, evo):
    vendedores("Ana", {0: "10:00-12:00"}, telefone="(11) 97777-6666")
    reuniao = svc.agendar(db, sp(12, 10), lead_nome="A", agora=AGORA, regras=REGRAS)
    assert svc.avisar_vendedor(db, reuniao) is None
    assert reuniao.vendedor_avisado_em is None
    assert evo.called("send_text") == []


def test_avisar_cancelamento(db, server, vendedores, evo):
    from app.services import numeros

    numero, _ = numeros.criar_numero(db, "Chip 1", server)
    numeros.aplicar_estado(db, numero, "open")
    db.commit()
    vendedores("Ana", {0: "10:00-12:00"}, telefone="(11) 97777-6666")
    reuniao = svc.agendar(db, sp(12, 10), lead_nome="Padaria X", agora=AGORA, regras=REGRAS)

    assert svc.avisar_cancelamento(db, reuniao) is None  # ainda agendada
    svc.avisar_vendedor(db, reuniao)
    svc.mudar_status(db, reuniao, ReuniaoStatus.CANCELADA)

    assert svc.avisar_cancelamento(db, reuniao) is not None
    (*_, number, text) = evo.called("send_text")[-1]
    assert number == "5511977776666"
    assert "Reunião cancelada" in text and "Padaria X" in text and "segunda, 12/10 às 10:00" in text
    assert svc.avisar_cancelamento(db, reuniao) is None  # não repete
    assert len(evo.called("send_text")) == 2


def test_cancelamento_sem_aviso_do_agendamento_nao_avisa(db, server, vendedores, evo):
    from app.services import numeros

    vendedores("Ana", {0: "10:00-12:00"}, telefone="(11) 97777-6666")
    reuniao = svc.agendar(db, sp(12, 10), lead_nome="A", agora=AGORA, regras=REGRAS)
    svc.avisar_vendedor(db, reuniao)  # sem número conectado: aviso fica pendente
    numero, _ = numeros.criar_numero(db, "Chip 1", server)
    numeros.aplicar_estado(db, numero, "open")
    svc.mudar_status(db, reuniao, ReuniaoStatus.CANCELADA)
    assert svc.avisar_cancelamento(db, reuniao) is None
    assert evo.called("send_text") == []
