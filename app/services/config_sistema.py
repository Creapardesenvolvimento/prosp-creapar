"""Parâmetros ajustáveis pelo painel, com padrão de fábrica.

Sem valor salvo no banco, vale o padrão (que, para a agenda e a rampa, vem do .env).
"""

import logging
import re
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Configuracao
from app.services.rampa import Rampa, parse_rampa

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Campo:
    chave: str
    grupo: str
    rotulo: str
    tipo: str  # dias | hora | inteiro | decimal | rampa
    ajuda: str = ""
    minimo: float | None = None
    maximo: float | None = None


CAMPOS = [
    Campo("disparo_dias", "Disparo", "Dias de disparo", "dias"),
    Campo("disparo_inicio", "Disparo", "Início do horário comercial", "hora"),
    Campo("disparo_fim", "Disparo", "Fim do horário comercial", "hora"),
    Campo("intervalo_min_minutos", "Disparo", "Intervalo mínimo entre envios (minutos)", "decimal",
          "Por número. O sistema sorteia um intervalo entre o mínimo e o máximo.", 0.5, 120),
    Campo("intervalo_max_minutos", "Disparo", "Intervalo máximo entre envios (minutos)", "decimal", "", 0.5, 240),
    Campo("rampa", "Disparo", "Rampa de aquecimento", "rampa",
          "dia_inicial:limite, separados por vírgula. Ex.: 1:10,4:20,8:40 = 10/dia nos dias 1–3, 20 nos dias 4–7…"),
    Campo("pausa_taxa_minima", "Proteção", "Pausar número com taxa de resposta abaixo de (%)", "decimal",
          "0 desliga a pausa automática.", 0, 100),
    Campo("pausa_janela_envios", "Proteção", "…medida nos últimos N envios", "inteiro",
          "Só conta envios com pelo menos 6 horas, para dar tempo de o lead responder.", 10, 1000),
    Campo("reuniao_minutos", "Agenda", "Duração da reunião (minutos)", "inteiro", "", 10, 240),
    Campo("reuniao_buffer_minutos", "Agenda", "Intervalo entre reuniões (minutos)", "inteiro", "", 0, 120),
    Campo("agenda_antecedencia_horas", "Agenda", "Antecedência mínima (horas)", "inteiro", "", 0, 72),
    Campo("agenda_dias_a_frente", "Agenda", "Oferecer horários até quantos dias à frente", "inteiro", "", 1, 60),
]
CAMPOS_POR_CHAVE = {c.chave: c for c in CAMPOS}


def padroes() -> dict:
    s = get_settings()
    return {
        "disparo_dias": [0, 1, 2, 3, 4],
        "disparo_inicio": "09:00",
        "disparo_fim": "18:00",
        "intervalo_min_minutos": 2.0,
        "intervalo_max_minutos": 6.0,
        "rampa": s.rampa_limites,
        "pausa_taxa_minima": 5.0,
        "pausa_janela_envios": 50,
        "reuniao_minutos": s.reuniao_minutos,
        "reuniao_buffer_minutos": s.reuniao_buffer_minutos,
        "agenda_antecedencia_horas": s.agenda_antecedencia_horas,
        "agenda_dias_a_frente": s.agenda_dias_a_frente,
    }


def valores(db: Session) -> dict:
    atuais = padroes()
    for linha in db.query(Configuracao).all():
        if linha.chave in atuais:
            atuais[linha.chave] = linha.valor
    return atuais


def _converter(campo: Campo, bruto) -> object:
    if campo.tipo == "dias":
        dias = sorted({int(d) for d in bruto}) if isinstance(bruto, list) else []
        if not dias or any(d not in range(7) for d in dias):
            raise ConfigError("Escolha pelo menos um dia de disparo.")
        return dias
    texto = str(bruto).strip()
    if campo.tipo == "hora":
        if not re.fullmatch(r"\d{1,2}:\d{2}", texto):
            raise ConfigError(f"{campo.rotulo}: use o formato 09:00.")
        hora, minuto = map(int, texto.split(":"))
        if hora > 23 or minuto > 59:
            raise ConfigError(f"{campo.rotulo}: horário inválido.")
        return f"{hora:02d}:{minuto:02d}"
    if campo.tipo == "rampa":
        try:
            parse_rampa(texto)
        except ValueError as exc:
            raise ConfigError(f"Rampa inválida: {exc}. Ex.: 1:10,4:20,8:40") from None
        return texto.replace(" ", "")
    try:
        numero = float(texto.replace(",", ".")) if campo.tipo == "decimal" else int(texto)
    except ValueError:
        raise ConfigError(f"{campo.rotulo}: informe um número.") from None
    if (campo.minimo is not None and numero < campo.minimo) or (campo.maximo is not None and numero > campo.maximo):
        raise ConfigError(f"{campo.rotulo}: use um valor entre {campo.minimo:g} e {campo.maximo:g}.")
    return numero


def salvar(db: Session, dados: dict) -> None:
    """dados: chave -> valor do formulário (lista para "dias"). Valida tudo antes de gravar."""
    novos = {chave: _converter(CAMPOS_POR_CHAVE[chave], bruto) for chave, bruto in dados.items() if chave in CAMPOS_POR_CHAVE}
    resultado = {**valores(db), **novos}
    if resultado["intervalo_min_minutos"] > resultado["intervalo_max_minutos"]:
        raise ConfigError("O intervalo mínimo não pode ser maior que o máximo.")
    if resultado["disparo_inicio"] >= resultado["disparo_fim"]:
        raise ConfigError("O início do horário comercial precisa ser antes do fim.")
    for chave, valor in novos.items():
        linha = db.get(Configuracao, chave)
        if linha is None:
            db.add(Configuracao(chave=chave, valor=valor))
        else:
            linha.valor = valor
    db.flush()
    logger.info("configurações salvas", extra={"chaves": sorted(novos)})


def restaurar_padroes(db: Session) -> None:
    db.query(Configuracao).delete()
    db.flush()
    logger.info("configurações restauradas para o padrão de fábrica")


# --- Leitura tipada -------------------------------------------------------------


@dataclass(frozen=True)
class RegrasDisparo:
    dias: frozenset[int]
    inicio: time
    fim: time
    intervalo_min_seg: float
    intervalo_max_seg: float
    taxa_minima: float  # em %
    janela_envios: int
    tz: ZoneInfo


def regras_disparo(db: Session) -> RegrasDisparo:
    v = valores(db)
    return RegrasDisparo(
        dias=frozenset(v["disparo_dias"]),
        inicio=time.fromisoformat(v["disparo_inicio"]),
        fim=time.fromisoformat(v["disparo_fim"]),
        intervalo_min_seg=float(v["intervalo_min_minutos"]) * 60,
        intervalo_max_seg=float(v["intervalo_max_minutos"]) * 60,
        taxa_minima=float(v["pausa_taxa_minima"]),
        janela_envios=int(v["pausa_janela_envios"]),
        tz=ZoneInfo(get_settings().timezone),
    )


def rampa(db: Session) -> Rampa:
    return parse_rampa(valores(db)["rampa"])
