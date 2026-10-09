import hmac
import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import WhatsappNumber
from app.services import numeros
from app.services.idempotency import body_key, register_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks")

# evento da Evolution (ex.: "connection.update") -> handler(db, payload)
EVOLUTION_HANDLERS: dict[str, Callable[[Session, dict], None]] = {}


def evolution_handler(event: str):
    def decorator(fn):
        EVOLUTION_HANDLERS[event] = fn
        return fn

    return decorator


@evolution_handler("connection.update")
def on_connection_update(db: Session, payload: dict) -> None:
    numero = db.scalar(select(WhatsappNumber).where(WhatsappNumber.instance_name == payload.get("instance")))
    if numero is None:
        logger.warning("connection.update de instância desconhecida", extra={"instance": payload.get("instance")})
        return
    data = payload.get("data") or {}
    numeros.aplicar_estado(db, numero, data.get("state"), data.get("statusReason"))


def _normalize_event(name: str) -> str:
    # A Evolution manda "connection.update" no corpo, mas "CONNECTION_UPDATE" na config.
    return name.strip().lower().replace("_", ".")


@router.post("/evolution/{token}")
async def evolution_webhook(token: str, request: Request, db: Session = Depends(get_db)):
    if not hmac.compare_digest(token, get_settings().webhook_token):
        raise HTTPException(status_code=404)

    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON inválido")

    event = _normalize_event(str(payload.get("event", "")))
    instance = payload.get("instance")

    if not register_event(db, "evolution", body_key(body)):
        logger.info("webhook duplicado ignorado", extra={"event": event, "instance": instance})
        return {"status": "duplicate"}

    handler = EVOLUTION_HANDLERS.get(event)
    if handler is None:
        logger.info("webhook sem handler", extra={"event": event, "instance": instance})
    else:
        logger.info("webhook recebido", extra={"event": event, "instance": instance})
        handler(db, payload)
    db.commit()
    return {"status": "ok"}
