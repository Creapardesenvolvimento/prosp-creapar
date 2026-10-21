from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import ETAPAS, Campanha, CampanhaStatus, Lead, LeadStatus, Mensagem, NumeroStatus, Porte, Roteiro, Segmento, User, WhatsappNumber
from app.services import agente, lead_teste, roteiros
from app.services.lead_teste import LeadTesteError
from app.integrations.evolution import EvolutionError
from app.services.agenda import como_utc, regras_padrao
from app.services.roteiros import ROTULO_ETAPA, ROTULO_PORTE, RoteiroError

router = APIRouter(dependencies=[Depends(current_user)])


# --- Roteiros ---------------------------------------------------------------------


@router.get("/roteiros")
def lista(request: Request, erro: str | None = None, ok: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    segmentos = db.scalars(select(Segmento).order_by(Segmento.id)).all()
    return templates.TemplateResponse(
        request, "roteiros/lista.html", {"user": user, "segmentos": segmentos, "erro": erro, "ok": ok}
    )


@router.post("/roteiros/importar")
async def importar(arquivo: UploadFile = File(...), db: Session = Depends(get_db)):
    conteudo = await arquivo.read()
    try:
        resultado = roteiros.importar(db, roteiros.ler_docx(conteudo))
    except RoteiroError as exc:
        db.rollback()
        return RedirectResponse(f"/roteiros?erro={quote(str(exc))}", status_code=303)
    db.commit()
    msg = f"{resultado['segmentos']} segmentos e {resultado['textos']} textos importados."
    return RedirectResponse(f"/roteiros?ok={quote(msg)}", status_code=303)


def _segmento(db: Session, segmento_id: int) -> Segmento:
    segmento = db.get(Segmento, segmento_id)
    if segmento is None:
        raise HTTPException(status_code=404)
    return segmento


@router.get("/roteiros/{segmento_id}")
def editar(segmento_id: int, request: Request, ok: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    segmento = _segmento(db, segmento_id)
    textos = {(r.porte, r.etapa): r for r in segmento.roteiros}
    focos = {r.porte: r.foco for r in segmento.roteiros}
    return templates.TemplateResponse(
        request,
        "roteiros/editar.html",
        {
            "user": user, "s": segmento, "textos": textos, "focos": focos, "ok": ok,
            "portes": list(Porte), "etapas": ETAPAS, "rotulo_porte": ROTULO_PORTE, "rotulo_etapa": ROTULO_ETAPA,
        },
    )


@router.post("/roteiros/{segmento_id}")
async def salvar(segmento_id: int, request: Request, db: Session = Depends(get_db)):
    segmento = _segmento(db, segmento_id)
    form = await request.form()
    segmento.palavras_chave = form.get("palavras_chave", "").strip()
    existentes = {(r.porte, r.etapa): r for r in segmento.roteiros}
    for porte in Porte:
        foco = form.get(f"foco_{porte}", "").strip()
        for etapa in ETAPAS:
            texto = form.get(f"texto_{porte}_{etapa}", "").strip()
            atual = existentes.get((porte, etapa))
            if texto and atual:
                atual.texto, atual.foco = texto, foco
            elif texto:
                segmento.roteiros.append(Roteiro(porte=porte, etapa=etapa, texto=texto, foco=foco))
            elif atual:
                segmento.roteiros.remove(atual)
    db.commit()
    return RedirectResponse(f"/roteiros/{segmento.id}?ok={quote('Roteiro salvo.')}", status_code=303)


# --- Leads em conversa ---------------------------------------------------------------

FILTROS = {
    "conversa": [LeadStatus.RESPONDEU, LeadStatus.EM_CONVERSA],
    "humano": [LeadStatus.HUMANO_ASSUMIU],
    "reuniao": [LeadStatus.REUNIAO_AGENDADA],
    "encerrados": [LeadStatus.SEM_INTERESSE, LeadStatus.OPT_OUT, LeadStatus.SEM_RESPOSTA, LeadStatus.JA_PROSPECTADO],
    "enviados": [LeadStatus.ENVIADO],
}


@router.get("/leads")
def leads(request: Request, filtro: str = "conversa", erro: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    status = FILTROS.get(filtro, FILTROS["conversa"])
    ultima = select(Mensagem.lead_id, func.max(Mensagem.id).label("ultima")).group_by(Mensagem.lead_id).subquery()
    lista = db.execute(
        select(Lead, ultima.c.ultima)
        .outerjoin(ultima, ultima.c.lead_id == Lead.id)
        .where(Lead.status.in_(status))
        .order_by(ultima.c.ultima.desc().nulls_last(), Lead.id.desc())
        .limit(200)
    ).all()
    contagem = {
        chave: db.scalar(select(func.count()).select_from(Lead).where(Lead.status.in_(valores))) for chave, valores in FILTROS.items()
    }
    return templates.TemplateResponse(
        request,
        "leads/lista.html",
        {
            "user": user, "leads": [l for l, _ in lista], "filtro": filtro, "contagem": contagem, "erro": erro,
            "numeros": db.scalars(select(WhatsappNumber).where(WhatsappNumber.status == NumeroStatus.CONECTADO).order_by(WhatsappNumber.nome_exibicao)).all(),
            "campanhas": db.scalars(select(Campanha).where(Campanha.status != CampanhaStatus.ENCERRADA).order_by(Campanha.created_at.desc())).all(),
            "segmentos": db.scalars(select(Segmento.nome).order_by(Segmento.id)).all(),
            "portes": list(Porte), "rotulo_porte": ROTULO_PORTE,
        },
    )


@router.post("/leads/teste")
async def criar_lead_teste(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    numero = db.get(WhatsappNumber, int(form.get("numero_id") or 0))
    campanha = db.get(Campanha, int(form.get("campanha_id") or 0))
    try:
        if numero is None or campanha is None:
            raise LeadTesteError("Escolha o número e a campanha.")
        lead = lead_teste.criar(
            db, nome=form.get("nome", ""), telefone=form.get("telefone", ""), numero=numero, campanha=campanha,
            categoria=form.get("categoria", ""), cidade=form.get("cidade", ""),
            segmento=form.get("segmento", ""), porte=form.get("porte", ""),
        )
    except (LeadTesteError, EvolutionError) as exc:
        db.rollback()
        return RedirectResponse(f"/leads?erro={quote(str(exc))}#teste", status_code=303)
    db.commit()
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.get("/leads/{lead_id}")
def lead(lead_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    historico = db.scalars(select(Mensagem).where(Mensagem.lead_id == lead.id).order_by(Mensagem.id)).all()
    return templates.TemplateResponse(
        request,
        "leads/detalhe.html",
        {
            "user": user, "lead": lead, "historico": historico,
            "rotulo_etapa": ROTULO_ETAPA, "rotulo_porte": ROTULO_PORTE, "local": _local,
        },
    )


def _local(valor):
    return como_utc(valor).astimezone(regras_padrao().tz)


@router.post("/leads/{lead_id}/devolver-ia")
def devolver_ia(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    agente.devolver_para_ia(db, lead)
    db.commit()
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/leads/{lead_id}/pausar-ia")
def pausar_ia(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    agente.passar_para_humano(db, lead, "IA pausada pelo time no painel")
    db.commit()
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)
