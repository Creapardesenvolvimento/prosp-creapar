from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.db import get_db
from app.models import (
    Blacklist,
    Busca,
    BuscaStatus,
    Campanha,
    CampanhaFollowup,
    CampanhaMensagem,
    Lead,
    LeadStatus,
    NumeroStatus,
    User,
    WhatsappNumber,
)
from app.services import campanhas as svc
from app.services import config_sistema, followup
from app.services.campanhas import CampanhaError
from app.services.config_sistema import CAMPOS, ConfigError
from app.services.ia import IAError
from app.services.mensagens import adicionar_blacklist
from app.services.telefone import normalizar_telefone

router = APIRouter(dependencies=[Depends(current_user)])


def _campanha(db: Session, campanha_id: int) -> Campanha:
    campanha = db.get(Campanha, campanha_id)
    if campanha is None:
        raise HTTPException(status_code=404)
    return campanha


def _voltar(campanha_id: int, erro: str | None = None, ancora: str = "") -> RedirectResponse:
    url = f"/campanhas/{campanha_id}"
    if erro:
        url += f"?erro={quote(erro)}"
    return RedirectResponse(url + ancora, status_code=303)


@router.get("/campanhas")
def lista(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lista = db.scalars(select(Campanha).order_by(Campanha.created_at.desc())).all()
    return templates.TemplateResponse(
        request,
        "campanhas/lista.html",
        {"user": user, "campanhas": [(c, svc.metricas(db, c), svc.progresso(db, c)) for c in lista]},
    )


@router.get("/campanhas/nova")
def nova(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "campanhas/nova.html", {"user": user, "erro": None, "v": {}})


@router.post("/campanhas")
async def criar(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    dados = {k: form.get(k, "") for k in ("nome", "nicho", "oferta", "prompt_agente")}
    try:
        campanha = svc.salvar(db, None, **dados)
    except CampanhaError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request, "campanhas/nova.html", {"user": user, "erro": str(exc), "v": dados}, status_code=400
        )
    db.commit()
    return RedirectResponse(f"/campanhas/{campanha.id}", status_code=303)


def _followup(db: Session, campanha: Campanha) -> dict:
    ativo, dias = followup.regras(db)
    rotulos = {"lembrete": "lembrete", "retomada": "retomada", "despedida": "despedida"}
    passos = [
        {
            "passo": i,
            "dia": dia,
            "papel": rotulos[followup.papel_do_passo(i, len(dias))],
            "proprios": [f for f in campanha.followups if f.passo == i],
            "fabrica": followup.textos_de_fabrica(db, i, len(dias)),
        }
        for i, dia in enumerate(dias, 1)
    ]
    return {"ativo": ativo, "passos": passos}


@router.get("/campanhas/{campanha_id}")
def detalhe(campanha_id: int, request: Request, erro: str | None = None, ok: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    campanha = _campanha(db, campanha_id)
    numeros = db.scalars(
        select(WhatsappNumber).where(WhatsappNumber.status.not_in([NumeroStatus.BANIDO, NumeroStatus.REMOVIDO])).order_by(WhatsappNumber.nome_exibicao)
    ).all()
    disponiveis = dict(
        db.execute(
            select(Lead.busca_id, func.count())
            .where(Lead.status == LeadStatus.NOVO, Lead.campanha_id.is_(None))
            .group_by(Lead.busca_id)
        ).all()
    )
    buscas = db.scalars(select(Busca).where(Busca.status == BuscaStatus.CONCLUIDA).order_by(Busca.created_at.desc())).all()
    return templates.TemplateResponse(
        request,
        "campanhas/detalhe.html",
        {
            "user": user,
            "c": campanha,
            "m": svc.metricas(db, campanha),
            "p": svc.progresso(db, campanha),
            "atualizar": True,
            "numeros": numeros,
            "selecionados": {n.id for n in campanha.numeros},
            "fu": _followup(db, campanha),
            "buscas": [(b, disponiveis.get(b.id, 0)) for b in buscas],
            "erro": erro,
            "ok": ok,
        },
    )


@router.get("/campanhas/{campanha_id}/progresso")
def progresso(campanha_id: int, request: Request, db: Session = Depends(get_db)):
    """Parcial atualizada pelo HTMX enquanto a página da campanha está aberta."""
    campanha = _campanha(db, campanha_id)
    return templates.TemplateResponse(
        request, "campanhas/_progresso.html", {"c": campanha, "p": svc.progresso(db, campanha), "atualizar": True}
    )


@router.post("/campanhas/{campanha_id}")
async def atualizar(campanha_id: int, request: Request, db: Session = Depends(get_db)):
    campanha = _campanha(db, campanha_id)
    form = await request.form()
    try:
        svc.salvar(db, campanha, **{k: form.get(k, "") for k in ("nome", "nicho", "oferta", "prompt_agente")})
    except CampanhaError as exc:
        db.rollback()
        return _voltar(campanha_id, str(exc))
    db.commit()
    return _voltar(campanha_id, ancora="#dados")


@router.post("/campanhas/{campanha_id}/mensagens")
async def adicionar_mensagem(campanha_id: int, request: Request, db: Session = Depends(get_db)):
    campanha = _campanha(db, campanha_id)
    form = await request.form()
    try:
        svc.adicionar_mensagem(db, campanha, form.get("texto", ""))
    except CampanhaError as exc:
        db.rollback()
        return _voltar(campanha_id, str(exc), "#mensagens")
    db.commit()
    return _voltar(campanha_id, ancora="#mensagens")


@router.post("/campanhas/{campanha_id}/mensagens/gerar")
def gerar_mensagens(campanha_id: int, db: Session = Depends(get_db)):
    campanha = _campanha(db, campanha_id)
    try:
        novas = svc.gerar_mensagens_ia(db, campanha)
    except IAError as exc:
        db.rollback()
        return _voltar(campanha_id, str(exc), "#mensagens")
    db.commit()
    return RedirectResponse(f"/campanhas/{campanha_id}?ok={quote(f'{novas} variações geradas pela IA. Revise antes de ativar.')}#mensagens", status_code=303)


@router.post("/campanhas/{campanha_id}/mensagens/{mensagem_id}/remover")
def remover_mensagem(campanha_id: int, mensagem_id: int, db: Session = Depends(get_db)):
    mensagem = db.get(CampanhaMensagem, mensagem_id)
    if mensagem is None or mensagem.campanha_id != campanha_id:
        raise HTTPException(status_code=404)
    svc.remover_mensagem(db, mensagem)
    db.commit()
    return _voltar(campanha_id, ancora="#mensagens")


@router.post("/campanhas/{campanha_id}/followups")
async def adicionar_followup(campanha_id: int, request: Request, db: Session = Depends(get_db)):
    campanha = _campanha(db, campanha_id)
    form = await request.form()
    try:
        svc.adicionar_followup(db, campanha, int(form.get("passo") or 0), form.get("texto", ""))
    except (CampanhaError, ValueError) as exc:
        db.rollback()
        return _voltar(campanha_id, str(exc) if isinstance(exc, CampanhaError) else "Escolha o passo.", "#followup")
    db.commit()
    return _voltar(campanha_id, ancora="#followup")


@router.post("/campanhas/{campanha_id}/followups/{followup_id}/remover")
def remover_followup(campanha_id: int, followup_id: int, db: Session = Depends(get_db)):
    item = db.get(CampanhaFollowup, followup_id)
    if item is None or item.campanha_id != campanha_id:
        raise HTTPException(status_code=404)
    db.delete(item)  # o histórico fica nos envios (o texto vai junto em cada um)
    db.commit()
    return _voltar(campanha_id, ancora="#followup")


@router.post("/campanhas/{campanha_id}/numeros")
async def definir_numeros(campanha_id: int, request: Request, db: Session = Depends(get_db)):
    campanha = _campanha(db, campanha_id)
    form = await request.form()
    svc.definir_numeros(db, campanha, [int(v) for v in form.getlist("numero_id")])
    db.commit()
    return _voltar(campanha_id, ancora="#numeros")


@router.post("/campanhas/{campanha_id}/leads")
async def adicionar_leads(campanha_id: int, request: Request, db: Session = Depends(get_db)):
    campanha = _campanha(db, campanha_id)
    form = await request.form()
    try:
        total = svc.adicionar_leads(db, campanha, [int(v) for v in form.getlist("busca_id")])
    except CampanhaError as exc:
        db.rollback()
        return _voltar(campanha_id, str(exc), "#leads")
    db.commit()
    return RedirectResponse(f"/campanhas/{campanha_id}?ok={quote(f'{total} leads entraram na fila.')}#leads", status_code=303)


@router.post("/campanhas/{campanha_id}/{acao}")
def acao(campanha_id: int, acao: str, db: Session = Depends(get_db)):
    funcoes = {"ativar": svc.ativar, "pausar": svc.pausar, "encerrar": svc.encerrar}
    if acao not in funcoes:
        raise HTTPException(status_code=404)
    campanha = _campanha(db, campanha_id)
    try:
        funcoes[acao](db, campanha)
    except CampanhaError as exc:
        db.rollback()
        return _voltar(campanha_id, str(exc))
    db.commit()
    return _voltar(campanha_id)


# --- Configurações e blacklist ------------------------------------------------


@router.get("/config")
def config(request: Request, erro: str | None = None, ok: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    grupos: dict[str, list] = {}
    for campo in CAMPOS:
        grupos.setdefault(campo.grupo, []).append(campo)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "user": user,
            "grupos": grupos,
            "v": config_sistema.valores(db),
            "padrao": config_sistema.padroes(),
            "dias": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"],
            "erro": erro,
            "ok": ok,
        },
    )


@router.post("/config")
async def salvar_config(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    if form.get("restaurar"):
        config_sistema.restaurar_padroes(db)
        db.commit()
        return RedirectResponse(f"/config?ok={quote('Padrões de fábrica restaurados.')}", status_code=303)
    # Dias vêm de checkboxes: nenhum marcado não aparece no formulário, mas é uma escolha (inválida).
    dados = {c.chave: form.getlist(c.chave) for c in CAMPOS if c.tipo == "dias"}
    # Checkbox desmarcado não vem no formulário.
    dados.update({c.chave: form.get(c.chave, "") for c in CAMPOS if c.tipo == "simnao"})
    dados.update({c.chave: form[c.chave] for c in CAMPOS if c.tipo not in ("dias", "simnao") and c.chave in form})
    try:
        config_sistema.salvar(db, dados)
    except ConfigError as exc:
        db.rollback()
        return RedirectResponse(f"/config?erro={quote(str(exc))}", status_code=303)
    db.commit()
    return RedirectResponse(f"/config?ok={quote('Configurações salvas.')}", status_code=303)


@router.get("/blacklist")
def blacklist(request: Request, erro: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    itens = db.scalars(select(Blacklist).order_by(Blacklist.created_at.desc()).limit(500)).all()
    total = db.scalar(select(func.count()).select_from(Blacklist))
    return templates.TemplateResponse(request, "blacklist.html", {"user": user, "itens": itens, "total": total, "erro": erro})


@router.post("/blacklist")
async def adicionar_na_blacklist(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    telefone = normalizar_telefone(form.get("telefone", ""))
    if telefone is None:
        return RedirectResponse(f"/blacklist?erro={quote('Telefone inválido.')}", status_code=303)
    adicionar_blacklist(db, telefone, (form.get("motivo") or "manual").strip()[:200])
    lead = db.scalar(select(Lead).where(Lead.telefone == telefone))
    if lead is not None:
        lead.status = LeadStatus.OPT_OUT
    db.commit()
    return RedirectResponse("/blacklist", status_code=303)
