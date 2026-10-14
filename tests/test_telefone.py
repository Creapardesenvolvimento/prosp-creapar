import pytest

from app.services.telefone import eh_celular, normalizar_telefone, para_evolution


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("(11) 99999-8888", "+5511999998888"),
        ("11999998888", "+5511999998888"),
        ("+55 11 99999-8888", "+5511999998888"),
        ("55 11 99999 8888", "+5511999998888"),
        ("011 99999-8888", "+5511999998888"),
        ("(11) 3333-4444", "+551133334444"),
        ("+55 (21) 98765-4321", "+5521987654321"),
    ],
)
def test_normaliza_para_e164(entrada, esperado):
    assert normalizar_telefone(entrada) == esperado


@pytest.mark.parametrize("entrada", [None, "", "abc", "9999-8888", "+1 415 555 0100", "(00) 99999-8888", "123456789012345"])
def test_rejeita_invalidos(entrada):
    assert normalizar_telefone(entrada) is None


def test_eh_celular():
    assert eh_celular("+5511999998888")
    assert not eh_celular("+551133334444")  # fixo
    assert not eh_celular("+5511899998888")  # 11 dígitos sem o 9
    assert not eh_celular(None)


def test_para_evolution():
    assert para_evolution("+5511999998888") == "5511999998888"
