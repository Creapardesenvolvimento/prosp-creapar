from fastapi import APIRouter, Depends, Request

from app.api.deps import current_user, templates
from app.models import User

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})
