import csv
import io
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import Busca, Campanha, CampanhaStatus, Lead, User
from app.services import base_empresas, campanhas
from app.services.agente import desqualificar
from app.services.base_empresas import Filtros
from app.services.campanhas import CampanhaError
from app.services.situacao import SITUACOES

router = APIRouter(dependencies=[Depends(current_user)])


def _filtros(situacao: str = "", q: str = "", lista: str = "") -> Filtros:
    return Filtros(situacao=situacao, texto=q, lista=int(lista) if lista.isdigit() else None)


def _query(f: Filtros, **extra) -> str:
    dados = {"situacao": f.situacao, "q": f.texto, "lista": f.lista or "", **extra}
    return urlencode({k: v for k, v in dados.items() if v not in ("", None)})


@router.get("/empresas")
def lista(
    request: Request,
    situacao: str = "",
    q: str = "",
    lista: str = "",
    pagina: int = 1,
    erro: str | None = None,
    ok: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    f = _filtros(situacao, q, lista)
    linhas, total = base_empresas.listar(db, f, pagina)
    contagens = base_empresas.contagens(db, f)
    return templates.TemplateResponse(
        request,
        "empresas/lista.html",
        {
            "user": user,
            "f": f,
            "linhas": linhas,
            "total": total,
            "pagina": max(pagina, 1),
            "paginas": max(-(-total // base_empresas.POR_PAGINA), 1),
            "contagens": contagens,
            "total_geral": sum(contagens.values()),
            "situacoes": SITUACOES,
            "listas": db.scalars(select(Busca).order_by(Busca.created_at.desc()).limit(200)).all(),
            "campanhas": db.scalars(
                select(Campanha).where(Campanha.status != CampanhaStatus.ENCERRADA).order_by(Campanha.created_at.desc())
            ).all(),
            "campanha_de": base_empresas.campanhas_dos_leads(db, [l for _, l, _ in linhas]),
            "query": lambda **extra: _query(f, **extra),
            "erro": erro,
            "ok": ok,
        },
    )


@router.post("/empresas/para-campanha")
def para_campanha(
    campanha_id: str = Form(""),
    q: str = Form(""),
    lista: str = Form(""),
    db: Session = Depends(get_db),
):
    f = _filtros("nao_contatado", q, lista)
    campanha = db.get(Campanha, int(campanha_id)) if campanha_id.isdigit() else None
    try:
        if campanha is None:
            raise CampanhaError("Escolha a campanha.")
        total = campanhas.adicionar_leads_por_id(db, campanha, base_empresas.leads_nao_contatados(db, f))
    except CampanhaError as exc:
        db.rollback()
        return RedirectResponse(f"/empresas?{_query(f, erro=str(exc))}", status_code=303)
    db.commit()
    texto = f"{total} empresa(s) entraram na fila da campanha {campanha.nome}."
    return RedirectResponse(f"/empresas?{_query(f, ok=texto)}", status_code=303)


@router.post("/leads/{lead_id}/desqualificar")
def desqualificar_lead(lead_id: int, motivo: str = Form(""), voltar: str = Form(""), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    desqualificar(lead, f"manual: {motivo.strip()}" if motivo.strip() else "manual")
    db.commit()
    destino = voltar if voltar.startswith(("/empresas", "/leads")) else f"/leads/{lead.id}"
    return RedirectResponse(destino, status_code=303)


COLUNAS_CSV = ["Empresa", "Telefone", "Situação", "Contatado", "Follow-ups enviados", "Campanha", "Cidade", "UF",
               "Categoria", "Site", "Lista", "Google Maps"]


def _texto_celula(valor):
    if isinstance(valor, str) and valor[:1] in ("=", "+", "-", "@"):
        return "'" + valor  # o Excel não transforma em fórmula e mantém o +55
    return "" if valor is None else valor


@router.get("/empresas.csv")
def exportar(situacao: str = "", q: str = "", lista: str = "", db: Session = Depends(get_db)):
    f = _filtros(situacao, q, lista)
    linhas = base_empresas.todas(db, f)
    campanha_de = base_empresas.campanhas_dos_leads(db, [l for _, l, _ in linhas])
    listas = {b.id: b.rotulo for b in db.scalars(select(Busca))}
    saida = io.StringIO()
    writer = csv.writer(saida, delimiter=";")
    writer.writerow(COLUNAS_CSV)
    for e, l, s in linhas:
        writer.writerow([_texto_celula(v) for v in (
            e.nome, e.telefone or e.telefone_original, s.rotulo, "sim" if s.contatado else "não",
            l.followups_enviados if l else "", campanha_de[l.campanha_id].nome if l and l.campanha_id in campanha_de else "",
            e.cidade, e.estado, e.categoria, e.site, listas.get(e.busca_id, ""), e.url_maps,
        )])
    return Response(
        "﻿" + saida.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="empresas.csv"'},
    )
