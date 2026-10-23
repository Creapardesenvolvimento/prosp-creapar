from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import Cliente, User
from app.services import clientes as svc
from app.services.clientes import ClienteError

router = APIRouter(dependencies=[Depends(current_user)])


def _cliente(db: Session, cliente_id: int) -> Cliente:
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404)
    return cliente


def _voltar(erro: str | None = None, ok: str | None = None) -> RedirectResponse:
    if erro:
        return RedirectResponse(f"/clientes?erro={quote(erro)}", status_code=303)
    return RedirectResponse(f"/clientes?ok={quote(ok or '')}", status_code=303)


@router.get("/clientes")
def lista(request: Request, erro: str | None = None, ok: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request,
        "clientes/lista.html",
        {
            "user": user,
            "linhas": svc.resumo(db),
            "ativos": db.scalars(select(Cliente).where(Cliente.ativo).order_by(Cliente.nome)).all(),
            "erro": erro,
            "ok": ok,
        },
    )


@router.post("/clientes")
def criar(
    request: Request,
    nome: str = Form(""),
    empresa_nome: str = Form(""),
    atendente_nome: str = Form(""),
    economia_texto: str = Form(""),
    copiar_de: str = Form(""),
    copiar_roteiros: str = Form(""),
    db: Session = Depends(get_db),
):
    origem = db.get(Cliente, int(copiar_de)) if copiar_de.isdigit() else None
    try:
        cliente = svc.criar(
            db, nome=nome, empresa_nome=empresa_nome, atendente_nome=atendente_nome, economia_texto=economia_texto,
            copiar_de=origem, copiar_roteiros=bool(copiar_roteiros),
        )
    except ClienteError as exc:
        db.rollback()
        return _voltar(erro=str(exc))
    db.commit()
    request.session["cliente_id"] = cliente.id  # já abre o painel no cliente novo
    return RedirectResponse("/numeros", status_code=303)


@router.post("/cliente/trocar")
def trocar(request: Request, cliente_id: str = Form(""), db: Session = Depends(get_db)):
    """Seletor do topo: o painel inteiro passa a mostrar o cliente escolhido."""
    cliente = db.get(Cliente, int(cliente_id)) if cliente_id.isdigit() else None
    if cliente is None or not cliente.ativo:
        return _voltar(erro="Cliente não encontrado ou arquivado.")
    request.session["cliente_id"] = cliente.id
    # Volta para a mesma área (sem o id de um registro, que é do cliente anterior).
    partes = [p for p in urlparse(request.headers.get("referer", "")).path.split("/") if p]
    area = f"/{partes[0]}" if partes and partes[0] not in ("cliente", "login") else "/"
    return RedirectResponse(area, status_code=303)


@router.post("/clientes/{cliente_id}/{acao}")
def acao(request: Request, cliente_id: int, acao: str, nome: str = Form(""), confirmacao: str = Form(""), db: Session = Depends(get_db)):
    cliente = _cliente(db, cliente_id)
    try:
        if acao == "renomear":
            svc.renomear(db, cliente, nome)
            texto = "Nome alterado."
        elif acao == "arquivar":
            svc.arquivar(db, cliente)
            texto = f"{cliente.nome} arquivado: campanhas pausadas e disparos parados."
        elif acao == "reativar":
            svc.reativar(db, cliente)
            texto = f"{cliente.nome} reativado. As campanhas continuam pausadas até você ativar."
        elif acao == "excluir":
            nome_cliente = cliente.nome
            removidos = svc.excluir(db, cliente, confirmacao)
            texto = f"{nome_cliente} excluído, com todos os dados. {removidos} número(s) desconectado(s) da Evolution."
        else:
            raise HTTPException(status_code=404)
    except ClienteError as exc:
        db.rollback()
        return _voltar(erro=str(exc))
    db.commit()
    if acao in ("arquivar", "excluir") and request.session.get("cliente_id") == cliente_id:
        request.session.pop("cliente_id", None)
    return _voltar(ok=texto)
