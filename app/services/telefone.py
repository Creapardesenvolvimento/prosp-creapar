"""Normalização de telefones brasileiros para E.164."""

import re


def normalizar_telefone(valor: str | None) -> str | None:
    """ "(11) 99999-8888", "+55 11 99999-8888", "011999998888" -> "+5511999998888".

    Retorna None se não for um número brasileiro (fixo ou celular) válido.
    """
    digitos = re.sub(r"\D", "", valor or "")
    if (valor or "").strip().startswith("+") and not digitos.startswith("55"):
        return None  # DDI de outro país
    if digitos.startswith("0"):  # prefixo de longa distância (0 + DDD)
        digitos = digitos.lstrip("0")
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if not (digitos.startswith("55") and len(digitos) in (12, 13)):
        return None
    ddd = digitos[2:4]
    if ddd[0] == "0" or ddd[1] == "0":
        return None
    return f"+{digitos}"


def eh_celular(e164: str | None) -> bool:
    """Celular: +55 + DDD + 9 + 8 dígitos."""
    return bool(e164 and re.fullmatch(r"\+55[1-9]{2}9\d{8}", e164))


def para_evolution(e164: str) -> str:
    """A Evolution recebe o número só com dígitos (DDI + DDD + número)."""
    return e164.lstrip("+")
