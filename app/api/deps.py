import hashlib
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Cliente, User
from app.services.situacao import situacao
from app.tenancy import usar_cliente

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["situacao"] = situacao  # rótulo da situação do lead/empresa em qualquer tela
# Muda a cada deploy que altera o CSS, para o navegador não usar a versão antiga em cache.
templates.env.globals["static_v"] = hashlib.md5(Path("app/static/app.css").read_bytes()).hexdigest()[:8]


class NotAuthenticated(Exception):
    pass


class SemClienteAtivo(Exception):
    """Nenhum cliente ativo: só a tela de Clientes funciona (para criar ou reativar um)."""


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.ativo:
        request.session.clear()
        raise NotAuthenticated
    if escolher_cliente(request, db) is None and not request.url.path.startswith(("/clientes", "/cliente/")):
        raise SemClienteAtivo
    return user


def escolher_cliente(request: Request, db: Session) -> Cliente | None:
    """Põe na sessão do banco o cliente escolhido no painel (ou o primeiro ativo) e o deixa à mão das telas."""
    ativos = list(db.scalars(select(Cliente).where(Cliente.ativo).order_by(Cliente.nome)))
    escolhido = next((c for c in ativos if c.id == request.session.get("cliente_id")), None) or (ativos[0] if ativos else None)
    if escolhido is not None:
        request.session["cliente_id"] = escolhido.id
    usar_cliente(db, escolhido.id if escolhido else None)
    request.state.cliente = escolhido
    request.state.clientes = ativos
    return escolhido
