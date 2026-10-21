import logging
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.config import get_settings
from app.db import get_db
from app.integrations.evolution import EvolutionError
from app.models import EvolutionServer, NumeroStatus, User, WhatsappNumber
from app.services import numeros as svc
from app.services.numeros import NumeroError
from app.services.rampa import dia_de_rampa
from app.tasks.numeros import marcar_conversas_numero_perdido

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])


def _numero(db: Session, numero_id: int) -> WhatsappNumber:
    numero = db.get(WhatsappNumber, numero_id)
    if numero is None:
        raise HTTPException(status_code=404)
    return numero


def _dia_de_rampa(numero: WhatsappNumber) -> int | None:
    if numero.data_ativacao is None:
        return None
    return dia_de_rampa(numero.data_ativacao, datetime.now(timezone.utc), get_settings().timezone)


@router.get("/numeros")
def lista(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    numeros = db.scalars(select(WhatsappNumber).order_by(WhatsappNumber.created_at.desc())).all()
    return templates.TemplateResponse(
        request,
        "numeros/lista.html",
        {
            "user": user,
            "numeros": [n for n in numeros if n.status != NumeroStatus.REMOVIDO],
            "removidos": [n for n in numeros if n.status == NumeroStatus.REMOVIDO],
            "alertas": svc.numeros_com_alerta(db),
            "pausados_auto": svc.numeros_pausados_automaticamente(db),
            "dia_de_rampa": _dia_de_rampa,
        },
    )


def _form_novo(request, db, user, substitui_id, erro=None, nome="", status_code=200):
    svc.garantir_servidor_padrao(db)
    db.commit()
    return templates.TemplateResponse(
        request,
        "numeros/novo.html",
        {
            "user": user,
            "servers": db.scalars(select(EvolutionServer).where(EvolutionServer.ativo)).all(),
            "substitui": db.get(WhatsappNumber, substitui_id) if substitui_id else None,
            "erro": erro,
            "nome": nome,
        },
        status_code=status_code,
    )


@router.get("/numeros/novo")
def novo(
    request: Request,
    substitui: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _form_novo(request, db, user, substitui)


@router.post("/numeros")
def criar(
    request: Request,
    nome_exibicao: str = Form(...),
    server_id: int = Form(...),
    substitui_numero_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    server = db.get(EvolutionServer, server_id)
    substitui = db.get(WhatsappNumber, substitui_numero_id) if substitui_numero_id else None
    try:
        if server is None:
            raise NumeroError("Servidor Evolution não encontrado.")
        numero, _ = svc.criar_numero(db, nome_exibicao, server, substitui)
    except (NumeroError, EvolutionError) as exc:
        db.rollback()
        return _form_novo(request, db, user, substitui_numero_id, erro=str(exc), nome=nome_exibicao, status_code=400)
    db.commit()
    return RedirectResponse(f"/numeros/{numero.id}", status_code=303)


@router.get("/numeros/{numero_id}")
def detalhe(
    numero_id: int,
    request: Request,
    erro: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    numero = _numero(db, numero_id)
    substituto = db.scalar(select(WhatsappNumber).where(WhatsappNumber.substitui_numero_id == numero.id))
    return templates.TemplateResponse(
        request,
        "numeros/detalhe.html",
        {"user": user, "numero": numero, "substituto": substituto, "dia": _dia_de_rampa(numero), "erro": erro},
    )


@router.get("/numeros/{numero_id}/qr")
def qr(numero_id: int, request: Request, db: Session = Depends(get_db)):
    """Parcial consultada pelo HTMX a cada poucos segundos enquanto o QR não é lido."""
    numero = _numero(db, numero_id)
    qr_base64, erro = None, None
    if numero.status == NumeroStatus.AGUARDANDO_QR:
        try:
            qr_base64 = svc.atualizar_qr(db, numero)
            db.commit()
        except EvolutionError as exc:
            db.rollback()
            erro = str(exc)
    headers = {} if numero.status == NumeroStatus.AGUARDANDO_QR else {"HX-Refresh": "true"}
    return templates.TemplateResponse(
        request, "numeros/_qr.html", {"numero": numero, "qr": qr_base64, "erro": erro}, headers=headers
    )


def _acao(db: Session, numero_id: int, fn, destino: str | None = None) -> RedirectResponse:
    numero = _numero(db, numero_id)
    try:
        fn(db, numero)
    except (NumeroError, EvolutionError) as exc:
        db.rollback()
        return RedirectResponse(f"/numeros/{numero.id}?erro={quote(str(exc))}", status_code=303)
    db.commit()
    return RedirectResponse(destino or f"/numeros/{numero.id}", status_code=303)


@router.post("/numeros/{numero_id}/pausar")
def pausar(numero_id: int, db: Session = Depends(get_db)):
    return _acao(db, numero_id, svc.pausar)


@router.post("/numeros/{numero_id}/retomar")
def retomar(numero_id: int, db: Session = Depends(get_db)):
    return _acao(db, numero_id, svc.retomar)


@router.post("/numeros/{numero_id}/reconectar")
def reconectar(numero_id: int, db: Session = Depends(get_db)):
    return _acao(db, numero_id, svc.reconectar)


@router.post("/numeros/{numero_id}/remover")
def remover(numero_id: int, db: Session = Depends(get_db)):
    return _acao(db, numero_id, svc.remover)


@router.post("/numeros/{numero_id}/proxy")
def salvar_proxy(
    numero_id: int,
    protocolo: str = Form("http"),
    host: str = Form(""),
    porta: str = Form(""),
    usuario: str = Form(""),
    senha: str = Form(""),
    db: Session = Depends(get_db),
):
    return _acao(
        db, numero_id,
        lambda db, n: svc.definir_proxy(db, n, protocolo=protocolo, host=host, porta=porta, usuario=usuario, senha=senha),
    )


@router.post("/numeros/{numero_id}/proxy/remover")
def remover_proxy(numero_id: int, db: Session = Depends(get_db)):
    return _acao(db, numero_id, svc.remover_proxy)


@router.post("/numeros/{numero_id}/banir")
def banir(numero_id: int, db: Session = Depends(get_db)):
    """Marca como banido/perdido e já abre o cadastro do número substituto."""
    destino = f"/numeros/novo?substitui={numero_id}"
    response = _acao(db, numero_id, svc.marcar_banido, destino)
    if response.headers["location"] == destino:
        marcar_conversas_numero_perdido.delay(numero_id)
    return response


@router.get("/servidores")
def servidores(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    svc.garantir_servidor_padrao(db)
    db.commit()
    servers = db.scalars(select(EvolutionServer).order_by(EvolutionServer.id)).all()
    return templates.TemplateResponse(request, "servidores.html", {"user": user, "servers": servers})


@router.post("/servidores")
def criar_servidor(
    nome: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(...),
    db: Session = Depends(get_db),
):
    db.add(EvolutionServer(nome=nome.strip(), base_url=base_url.strip().rstrip("/"), api_key=api_key.strip()))
    db.commit()
    logger.info("servidor Evolution cadastrado", extra={"nome": nome, "base_url": base_url})
    return RedirectResponse("/servidores", status_code=303)


@router.post("/servidores/{server_id}/alternar")
def alternar_servidor(server_id: int, db: Session = Depends(get_db)):
    server = db.get(EvolutionServer, server_id)
    if server is None:
        raise HTTPException(status_code=404)
    server.ativo = not server.ativo
    db.commit()
    return RedirectResponse("/servidores", status_code=303)
