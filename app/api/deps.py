import hashlib
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User

templates = Jinja2Templates(directory="app/templates")
# Muda a cada deploy que altera o CSS, para o navegador não usar a versão antiga em cache.
templates.env.globals["static_v"] = hashlib.md5(Path("app/static/app.css").read_bytes()).hexdigest()[:8]


class NotAuthenticated(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.ativo:
        request.session.clear()
        raise NotAuthenticated
    return user
