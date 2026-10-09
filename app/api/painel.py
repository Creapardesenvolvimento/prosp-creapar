from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import User
from app.services import numeros as svc

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "alertas": svc.numeros_com_alerta(db), "no_rodizio": len(svc.numeros_no_rodizio(db))},
    )
