"""Rampa de aquecimento: limite diário de envios conforme a idade do chip."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings

Rampa = list[tuple[int, int]]


def parse_rampa(spec: str) -> Rampa:
    """ "1:10,4:20,8:40" -> [(1, 10), (4, 20), (8, 40)] """
    rampa = sorted((int(dia), int(limite)) for dia, limite in (item.split(":") for item in spec.split(",")))
    if not rampa or rampa[0][0] != 1:
        raise ValueError("a rampa precisa começar no dia 1")
    if any(limite < 0 for _, limite in rampa):
        raise ValueError("limite diário não pode ser negativo")
    return rampa


def limite_para_dia(dia: int, rampa: Rampa) -> int:
    limite = 0
    for dia_inicial, valor in rampa:
        if dia >= dia_inicial:
            limite = valor
    return limite


def dia_de_rampa(data_ativacao: datetime, agora: datetime, tz: str) -> int:
    """Dia 1 é o dia (no fuso configurado) em que o número conectou."""
    zone = ZoneInfo(tz)

    def local_date(value: datetime):
        if value.tzinfo is None:  # SQLite devolve datetimes sem fuso; gravamos sempre em UTC
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(zone).date()

    return (local_date(agora) - local_date(data_ativacao)).days + 1


def limite_do_numero(data_ativacao: datetime | None, agora: datetime | None = None) -> int:
    if data_ativacao is None:
        return 0
    settings = get_settings()
    agora = agora or datetime.now(timezone.utc)
    dia = dia_de_rampa(data_ativacao, agora, settings.timezone)
    return limite_para_dia(dia, parse_rampa(settings.rampa_limites))
