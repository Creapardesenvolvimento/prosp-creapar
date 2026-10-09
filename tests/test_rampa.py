from datetime import datetime, timezone

import pytest

from app.services.rampa import dia_de_rampa, limite_do_numero, limite_para_dia, parse_rampa

RAMPA = parse_rampa("1:10,4:20,8:40")
TZ = "America/Sao_Paulo"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_parse_ordena_as_faixas():
    assert parse_rampa("8:40, 1:10 ,4:20") == [(1, 10), (4, 20), (8, 40)]


@pytest.mark.parametrize("spec", ["", "2:10", "1:-5", "abc"])
def test_parse_rejeita_rampa_invalida(spec):
    with pytest.raises(ValueError):
        parse_rampa(spec)


@pytest.mark.parametrize(
    "dia, limite",
    [(0, 0), (1, 10), (3, 10), (4, 20), (7, 20), (8, 40), (365, 40)],
)
def test_limite_sobe_por_faixa_e_estabiliza(dia, limite):
    assert limite_para_dia(dia, RAMPA) == limite


def test_dia_1_e_o_dia_da_ativacao():
    ativacao = utc(2026, 3, 10, 15, 0)
    assert dia_de_rampa(ativacao, utc(2026, 3, 10, 20, 0), TZ) == 1
    assert dia_de_rampa(ativacao, utc(2026, 3, 13, 15, 0), TZ) == 4


def test_dia_vira_a_meia_noite_de_sao_paulo_e_nao_em_utc():
    ativacao = utc(2026, 3, 10, 15, 0)  # 12h em São Paulo
    assert dia_de_rampa(ativacao, utc(2026, 3, 11, 2, 59), TZ) == 1  # 23h59 do dia 10 em SP
    assert dia_de_rampa(ativacao, utc(2026, 3, 11, 3, 0), TZ) == 2  # 00h do dia 11 em SP


def test_datetime_sem_fuso_e_tratado_como_utc():
    assert dia_de_rampa(datetime(2026, 3, 10, 15, 0), utc(2026, 3, 11, 15, 0), TZ) == 2


def test_limite_do_numero():
    assert limite_do_numero(None) == 0
    ativacao = utc(2026, 3, 10, 15, 0)
    assert limite_do_numero(ativacao, utc(2026, 3, 10, 16, 0)) == 10
    assert limite_do_numero(ativacao, utc(2026, 3, 14, 16, 0)) == 20
    assert limite_do_numero(ativacao, utc(2026, 4, 30, 16, 0)) == 40
