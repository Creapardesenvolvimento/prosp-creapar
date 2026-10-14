import hmac
import json
import logging
from datetime import datetime, timezone
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Busca, WhatsappNumber
from app.services import buscas, mensagens, numeros
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


@evolution_handler("messages.upsert")
@evolution_handler("send.message")
def on_message(db: Session, payload: dict):
    """Mensagem recebida ou enviada pelo número. Retorna o que precisa rodar depois do commit."""
    from app.tasks.agente import conferir_mensagem_propria, responder_lead

    key = (payload.get("data") or {}).get("key") or {}
    # A Evolution pode reenviar a mesma mensagem com corpo diferente; o id dela é a chave.
    if key.get("id") and not register_event(db, "evolution-msg", f"{payload.get('instance')}:{key['id']}"):
        return None
    if key.get("fromMe"):
        dados = mensagens.registrar_propria(db, payload)
        if dados is None:
            return None
        return lambda: conferir_mensagem_propria.apply_async((dados,), countdown=15)
    lead = mensagens.registrar_recebida(db, payload)
    if lead is None:
        return None
    if lead.responder_em is not None:
        espera = max((lead.responder_em - datetime.now(timezone.utc)).total_seconds(), 0)
        lead_id = lead.id
        return lambda: responder_lead.apply_async((lead_id,), countdown=espera + 1)
    if not lead.ia_ativa and lead.handoff_motivo:
        from app.tasks.agente import rotular_conversa

        lead_id = lead.id
        return lambda: rotular_conversa.delay(lead_id, "humano")
    return None


def _normalize_event(name: str) -> str:
    # A Evolution manda "connection.update" no corpo, mas "CONNECTION_UPDATE" na config.
    return name.strip().lower().replace("_", ".")


async def _ler_payload(token: str, request: Request) -> tuple[bytes, dict]:
    if not hmac.compare_digest(token, get_settings().webhook_token):
        raise HTTPException(status_code=404)
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON inválido")
    return body, payload


@router.post("/evolution/{token}")
async def evolution_webhook(token: str, request: Request, db: Session = Depends(get_db)):
    body, payload = await _ler_payload(token, request)

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
        depois = handler(db, payload)
    db.commit()
    if handler is not None and callable(depois):
        depois()  # tarefas que dependem do que acabou de ser gravado
    return {"status": "ok"}


@router.post("/apify/{token}")
async def apify_webhook(token: str, request: Request, db: Session = Depends(get_db)):
    """Fim de um run do Apify. O polling de conferir_buscas cobre o caso de este aviso não chegar."""
    from app.tasks.buscas import processar_busca

    _, payload = await _ler_payload(token, request)
    run = payload.get("resource") or {}
    event = payload.get("eventType")
    if not run.get("id") or not register_event(db, "apify", f"{run['id']}:{event}"):
        return {"status": "duplicate"}

    busca = db.scalar(select(Busca).where(Busca.apify_run_id == run["id"]))
    if busca is None:
        logger.warning("webhook do Apify de run desconhecido", extra={"run_id": run["id"]})
        db.commit()
        return {"status": "ok"}
    logger.info("webhook do Apify recebido", extra={"busca_id": busca.id, "event": event, "status": run.get("status")})
    processar = buscas.registrar_fim_do_run(db, busca, run)
    db.commit()
    if processar:
        processar_busca.delay(busca.id)
    return {"status": "ok"}
