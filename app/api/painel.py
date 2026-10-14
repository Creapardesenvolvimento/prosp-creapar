from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import User
from sqlalchemy import select

from app.models import Campanha, CampanhaStatus
from app.services import campanhas
from app.services import numeros as svc

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "alertas": svc.numeros_com_alerta(db),
            "pausados_auto": svc.numeros_pausados_automaticamente(db),
            "no_rodizio": len(svc.numeros_no_rodizio(db)),
            "campanhas": [
                (c, campanhas.progresso(db, c))
                for c in db.scalars(
                    select(Campanha).where(Campanha.status.in_([CampanhaStatus.ATIVA, CampanhaStatus.PAUSADA])).order_by(Campanha.created_at.desc())
                )
            ],
        },
    )
