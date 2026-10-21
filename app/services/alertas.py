"""Avisos no WhatsApp de notificações quando um número de prospecção sai do rodízio.

Com muitos chips, ninguém fica olhando o painel o dia todo: o aviso chega uma vez por número,
motivo e dia. Falha ao avisar nunca atrapalha quem chamou.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.evolution import EvolutionError
from app.models import WhatsappNumber
from app.services import config_sistema

logger = logging.getLogger(__name__)


def mensagem_chip(numero: WhatsappNumber, situacao: str) -> str:
    linhas = [f"⚠️ *Número fora do rodízio*: {numero.nome_exibicao}"]
    if numero.telefone:
        linhas.append(f"Telefone: {numero.telefone}")
    linhas.append(f"Motivo: {situacao}")
    return "\n".join(linhas)


def avisar_chip(db: Session, numero: WhatsappNumber, tipo: str, situacao: str, agora: datetime | None = None) -> bool:
    """tipo agrupa o aviso (desconectado, pausado…); situacao é o texto para quem lê. Retorna True se avisou."""
    from app.services.agenda import enviar_whatsapp
    from app.services.idempotency import register_event

    v = config_sistema.valores(db)
    telefone = v["notificar_telefone"]
    if not (v["alertar_chips"] and telefone):
        return False
    agora = agora or datetime.now(timezone.utc)
    hoje = agora.astimezone(ZoneInfo(get_settings().timezone)).date().isoformat()
    try:
        with db.begin_nested():
            if not register_event(db, "alerta-chip", f"{numero.id}:{tipo}:{hoje}"):
                return False
            if enviar_whatsapp(db, telefone, mensagem_chip(numero, situacao), "alerta-chip", numero_id=numero.id) is None:
                raise EvolutionError(None, "nenhum número conectado")  # desfaz o registro: tenta de novo na próxima
    except EvolutionError as exc:
        logger.warning("não consegui avisar sobre o número", extra={"numero_id": numero.id, "erro": str(exc)})
        return False
    return True
