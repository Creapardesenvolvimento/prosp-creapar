import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import WebhookEvent


def body_key(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def register_event(db: Session, origem: str, chave: str) -> bool:
    """Marca o evento como visto. Retorna False se ele já tinha sido registrado.

    Não faz commit: o registro entra na mesma transação do processamento, então
    um evento que falhou no meio pode ser reprocessado no reenvio.
    """
    try:
        with db.begin_nested():
            db.add(WebhookEvent(origem=origem, chave=chave))
    except IntegrityError:
        return False
    return True
