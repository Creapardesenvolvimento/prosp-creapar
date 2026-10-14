import csv
import io
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.config import get_settings
from app.db import get_db
from app.models import Busca, BuscaStatus, Empresa, Lead, User
from app.services import buscas as svc
from app.services.buscas import MOTIVOS_DESCARTE, BuscaError
from app.tasks.buscas import iniciar_busca, processar_busca

router = APIRouter(dependencies=[Depends(current_user)])
EM_ANDAMENTO = (BuscaStatus.PENDENTE, BuscaStatus.EXECUTANDO, BuscaStatus.PROCESSANDO)


def _busca(db: Session, busca_id: int) -> Busca:
    busca = db.get(Busca, busca_id)
    if busca is None:
        raise HTTPException(status_code=404)
    return busca


def _contexto_form(db: Session, user: User, **extra) -> dict:
    settings = get_settings()
    return {
        "user": user,
        "buscas": db.scalars(select(Busca).order_by(Busca.created_at.desc()).limit(100)).all(),
        "preco": settings.apify_preco_por_lugar,
        "limite_maximo": settings.busca_limite_maximo,
        "apify_configurado": bool(settings.apify_token),
        "erro": None,
        "valores": {"termo": "", "cidade": "", "limite": 100},
        "parecidas": [],
        **extra,
    }


@router.get("/buscas")
def lista(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "buscas/lista.html", _contexto_form(db, user))


@router.post("/buscas")
def criar(
    request: Request,
    termo: str = Form(""),
    cidade: str = Form(""),
    limite: int = Form(100),
    confirmar: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    valores = {"termo": termo, "cidade": cidade, "limite": limite}
    parecidas = svc.buscas_parecidas(db, termo, cidade) if termo.strip() and cidade.strip() else []
    if parecidas and not confirmar:
        contexto = _contexto_form(db, user, valores=valores, parecidas=parecidas)
        return templates.TemplateResponse(request, "buscas/lista.html", contexto, status_code=409)
    try:
        busca = svc.criar_busca(db, termo, cidade, limite, user)
    except BuscaError as exc:
        db.rollback()
        contexto = _contexto_form(db, user, erro=str(exc), valores=valores)
        return templates.TemplateResponse(request, "buscas/lista.html", contexto, status_code=400)
    db.commit()
    iniciar_busca.delay(busca.id)
    return RedirectResponse(f"/buscas/{busca.id}", status_code=303)


def _contexto_detalhe(db: Session, busca: Busca) -> dict:
    leads = db.scalars(select(Lead).where(Lead.busca_id == busca.id).order_by(Lead.id).limit(200)).all()
    descartadas = db.scalars(
        select(Empresa).where(Empresa.busca_id == busca.id, Empresa.descarte.is_not(None)).order_by(Empresa.id).limit(300)
    ).all()
    return {
        "busca": busca,
        "leads": leads,
        "descartadas": descartadas,
        "tem_empresas": bool(
            db.scalar(select(Empresa.id).where(Empresa.busca_id == busca.id).limit(1))
        ),
        "motivos": MOTIVOS_DESCARTE,
        "descartados": sum((busca.descartes or {}).values()),
        "em_andamento": busca.status in EM_ANDAMENTO,
        "custo_maximo": svc.custo_maximo(busca.limite),
    }


@router.get("/buscas/{busca_id}")
def detalhe(busca_id: int, request: Request, erro: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    busca = _busca(db, busca_id)
    return templates.TemplateResponse(
        request, "buscas/detalhe.html", {"user": user, "erro_acao": erro, **_contexto_detalhe(db, busca)}
    )


@router.get("/buscas/{busca_id}/status")
def status(busca_id: int, request: Request, db: Session = Depends(get_db)):
    """Parcial atualizada pelo HTMX enquanto a busca está em andamento."""
    busca = _busca(db, busca_id)
    headers = {} if busca.status in EM_ANDAMENTO else {"HX-Refresh": "true"}
    return templates.TemplateResponse(request, "buscas/_status.html", _contexto_detalhe(db, busca), headers=headers)


@router.post("/buscas/{busca_id}/reprocessar")
def reprocessar(busca_id: int, db: Session = Depends(get_db)):
    busca = _busca(db, busca_id)
    try:
        svc.preparar_reprocessamento(db, busca)
    except BuscaError as exc:
        db.rollback()
        return RedirectResponse(f"/buscas/{busca.id}?erro={quote(str(exc))}", status_code=303)
    db.commit()
    processar_busca.delay(busca.id)
    return RedirectResponse(f"/buscas/{busca.id}", status_code=303)


@router.post("/buscas/{busca_id}/recuperar")
def recuperar(busca_id: int, db: Session = Depends(get_db)):
    busca = _busca(db, busca_id)
    try:
        svc.recuperar_empresas(db, busca)
    except BuscaError as exc:
        db.rollback()
        return RedirectResponse(f"/buscas/{busca.id}?erro={quote(str(exc))}", status_code=303)
    db.commit()
    return RedirectResponse(f"/buscas/{busca.id}", status_code=303)


COLUNAS_CSV = [
    ("Empresa", "nome"),
    ("Telefone", "telefone_original"),
    ("Telefone (padronizado)", "telefone"),
    ("Celular", "celular"),
    ("Situação", None),
    ("Categoria", "categoria"),
    ("Site", "site"),
    ("Endereço", "endereco"),
    ("Bairro", "bairro"),
    ("Cidade", "cidade"),
    ("Estado", "estado"),
    ("Nota", "nota"),
    ("Avaliações", "avaliacoes"),
    ("Google Maps", "url_maps"),
]


def _celula(valor):
    """Texto que começa com = + - @ vira fórmula no Excel; o apóstrofo força texto (e mantém o +55 do telefone)."""
    if isinstance(valor, str) and valor[:1] in ("=", "+", "-", "@"):
        return "'" + valor
    return valor


@router.get("/buscas/{busca_id}/empresas.csv")
def exportar(busca_id: int, db: Session = Depends(get_db)):
    """Todas as empresas da busca (as que viraram lead e as descartadas), em planilha para o Excel."""
    busca = _busca(db, busca_id)
    empresas = db.scalars(select(Empresa).where(Empresa.busca_id == busca.id).order_by(Empresa.id)).all()
    saida = io.StringIO()
    writer = csv.writer(saida, delimiter=";")  # ";" e BOM: o Excel em português abre direto
    writer.writerow([titulo for titulo, _ in COLUNAS_CSV])
    for e in empresas:
        situacao = "virou lead" if e.descarte is None and e.lead_id else MOTIVOS_DESCARTE.get(e.descarte, e.descarte or "")
        linha = []
        for _, campo in COLUNAS_CSV:
            if campo is None:
                linha.append(situacao)
            elif campo == "celular":
                linha.append("sim" if e.celular else "não")
            elif campo == "nota" and e.nota is not None:
                linha.append(str(e.nota).replace(".", ","))
            else:
                valor = getattr(e, campo)
                linha.append("" if valor is None else _celula(valor))
        writer.writerow(linha)
    nome = re.sub(r"[^a-z0-9]+", "-", f"{busca.termo}-{busca.cidade}".lower()).strip("-") or "busca"
    return Response(
        "\ufeff" + saida.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}-{busca.id}.csv"'},
    )
