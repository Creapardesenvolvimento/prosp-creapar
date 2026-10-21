from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import User
from app.services import config_sistema, prospeccao
from app.services.config_sistema import ConfigError
from app.services.numeros import virada_do_dia as recalcular_limites

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/prospeccao")
def pagina(
    request: Request,
    erro: str | None = None,
    ok: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return templates.TemplateResponse(
        request, "prospeccao/pagina.html", {"user": user, "p": prospeccao.painel(db), "erro": erro, "ok": ok}
    )


@router.get("/prospeccao/painel")
def painel(request: Request, db: Session = Depends(get_db)):
    """Parcial que o HTMX recarrega a cada 30 segundos."""
    return templates.TemplateResponse(request, "prospeccao/_painel.html", {"p": prospeccao.painel(db)})


@router.post("/prospeccao/metas")
def salvar_metas(
    meta_diaria: str = Form(""),
    teto_por_numero: str = Form(""),
    limite_hora_numero: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        config_sistema.salvar(
            db,
            {"meta_diaria": meta_diaria, "teto_por_numero": teto_por_numero, "limite_hora_numero": limite_hora_numero},
        )
    except ConfigError as exc:
        db.rollback()
        return RedirectResponse(f"/prospeccao?erro={quote(str(exc))}", status_code=303)
    recalcular_limites(db)  # o teto novo vale já hoje
    db.commit()
    return RedirectResponse(f"/prospeccao?ok={quote('Metas salvas.')}", status_code=303)
