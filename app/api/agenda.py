from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user, templates
from app.db import get_db
from app.integrations.evolution import EvolutionError
from app.models import Reuniao, ReuniaoStatus, User, Vendedor, VendedorBloqueio
from app.services import agenda as svc
from app.services import config_sistema
from app.services.agenda import DIAS_SEMANA, AgendaError, como_utc
from app.tasks.agenda import avisar_cancelamento, avisar_vendedor

router = APIRouter(dependencies=[Depends(current_user)])


def _vendedor(db: Session, vendedor_id: int) -> Vendedor:
    vendedor = db.get(Vendedor, vendedor_id)
    if vendedor is None:
        raise HTTPException(status_code=404)
    return vendedor


def _local(valor: datetime) -> datetime:
    return como_utc(valor).astimezone(svc.regras_padrao().tz)


@router.get("/vendedores")
def vendedores(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lista = db.scalars(select(Vendedor).options(selectinload(Vendedor.horarios)).order_by(Vendedor.nome)).all()
    return templates.TemplateResponse(
        request,
        "vendedores/lista.html",
        {"user": user, "vendedores": lista, "dias": DIAS_SEMANA, "faixas": svc.formatar_faixas},
    )


def _form(request, user, vendedor, valores, erro=None, status_code=200):
    return templates.TemplateResponse(
        request,
        "vendedores/form.html",
        {"user": user, "vendedor": vendedor, "v": valores, "dias": DIAS_SEMANA, "erro": erro, "local": _local},
        status_code=status_code,
    )


def _valores(vendedor: Vendedor | None) -> dict:
    if vendedor is None:
        return {"nome": "", "telefone": "", "email": "", "link_reuniao": "", "ativo": True, "faixas": {}}
    return {
        "nome": vendedor.nome,
        "telefone": vendedor.telefone or "",
        "email": vendedor.email or "",
        "link_reuniao": vendedor.link_reuniao or "",
        "ativo": vendedor.ativo,
        "faixas": {dia: svc.formatar_faixas(vendedor, dia) for dia in range(7)},
    }


@router.get("/vendedores/novo")
def novo_vendedor(request: Request, user: User = Depends(current_user)):
    return _form(request, user, None, _valores(None))


@router.get("/vendedores/{vendedor_id}")
def editar_vendedor(vendedor_id: int, request: Request, erro: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    vendedor = _vendedor(db, vendedor_id)
    return _form(request, user, vendedor, _valores(vendedor), erro=erro)


async def _salvar(request: Request, db: Session, user: User, vendedor: Vendedor | None):
    form = await request.form()
    valores = {
        "nome": form.get("nome", ""),
        "telefone": form.get("telefone", ""),
        "email": form.get("email", ""),
        "link_reuniao": form.get("link_reuniao", ""),
        "ativo": form.get("ativo") == "on",
        "faixas": {dia: form.get(f"faixas_{dia}", "") for dia in range(7)},
    }
    try:
        vendedor = svc.salvar_vendedor(
            db,
            vendedor,
            nome=valores["nome"],
            telefone=valores["telefone"],
            email=valores["email"],
            link_reuniao=valores["link_reuniao"],
            ativo=valores["ativo"],
            faixas_por_dia=valores["faixas"],
        )
    except AgendaError as exc:
        db.rollback()
        return _form(request, user, vendedor, valores, erro=str(exc), status_code=400)
    db.commit()
    return RedirectResponse(f"/vendedores/{vendedor.id}", status_code=303)


@router.post("/vendedores")
async def criar_vendedor(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return await _salvar(request, db, user, None)


@router.post("/vendedores/{vendedor_id}")
async def atualizar_vendedor(vendedor_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return await _salvar(request, db, user, _vendedor(db, vendedor_id))


@router.post("/vendedores/{vendedor_id}/bloqueios")
async def criar_bloqueio(vendedor_id: int, request: Request, db: Session = Depends(get_db)):
    vendedor = _vendedor(db, vendedor_id)
    form = await request.form()
    try:
        svc.adicionar_bloqueio(db, vendedor, form.get("inicio", ""), form.get("fim", ""), form.get("motivo", ""))
    except AgendaError as exc:
        db.rollback()
        return RedirectResponse(f"/vendedores/{vendedor.id}?erro={quote(str(exc))}", status_code=303)
    db.commit()
    return RedirectResponse(f"/vendedores/{vendedor.id}#bloqueios", status_code=303)


@router.post("/vendedores/{vendedor_id}/bloqueios/{bloqueio_id}/remover")
def remover_bloqueio(vendedor_id: int, bloqueio_id: int, db: Session = Depends(get_db)):
    bloqueio = db.get(VendedorBloqueio, bloqueio_id)
    if bloqueio is None or bloqueio.vendedor_id != vendedor_id:
        raise HTTPException(status_code=404)
    db.delete(bloqueio)
    db.commit()
    return RedirectResponse(f"/vendedores/{vendedor_id}#bloqueios", status_code=303)


@router.get("/agenda")
def agenda(
    request: Request,
    erro: str | None = None,
    vendedor_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    desde = datetime.now(timezone.utc) - timedelta(hours=12)
    query = select(Reuniao).where(Reuniao.inicio >= desde).order_by(Reuniao.inicio)
    if vendedor_id:
        query = query.where(Reuniao.vendedor_id == vendedor_id)
    livres = svc.horarios_livres(db)
    vendedores = {v.id: v for v in db.scalars(select(Vendedor).order_by(Vendedor.nome))}
    return templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "user": user,
            "reunioes": db.scalars(query).all(),
            "livres": livres[:40],
            "sugestao": " ou ".join(svc.formatar_horario(h.inicio) for h in svc.sugerir_horarios(livres)),
            "vendedores": vendedores,
            "vendedor_id": vendedor_id,
            "notificar": svc.telefone_notificacoes(db),
            "avisar_vendedor": config_sistema.valores(db)["avisar_vendedor"],
            "formatar": svc.formatar_horario,
            "local": _local,
            "erro": erro,
        },
    )


@router.post("/agenda")
async def agendar_manual(request: Request, db: Session = Depends(get_db)):
    """Agendamento feito pelo time no painel (a IA usa o mesmo serviço na Fase 5)."""
    form = await request.form()
    try:
        if not form.get("lead_nome", "").strip():
            raise AgendaError("Informe o nome do lead.")
        inicio = datetime.fromisoformat(form.get("inicio", ""))
        reuniao = svc.agendar(
            db,
            inicio,
            lead_nome=form["lead_nome"],
            lead_telefone=form.get("lead_telefone") or None,
            observacoes=form.get("observacoes", ""),
        )
    except (AgendaError, ValueError) as exc:
        db.rollback()
        mensagem = str(exc) if isinstance(exc, AgendaError) else "Escolha um horário."
        return RedirectResponse(f"/agenda?erro={quote(mensagem)}", status_code=303)
    db.commit()
    avisar_vendedor.delay(reuniao.id)
    return RedirectResponse("/agenda", status_code=303)


def _dia(data: str | None) -> date:
    hoje = datetime.now(timezone.utc).astimezone(svc.regras_padrao().tz).date()
    try:
        return date.fromisoformat(data) if data else hoje
    except ValueError:
        return hoje


@router.get("/agenda/dia")
def reunioes_do_dia(
    request: Request,
    data: str | None = None,
    erro: str | None = None,
    ok: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    dia = _dia(data)
    hoje = _dia(None)
    return templates.TemplateResponse(
        request,
        "reunioes_dia.html",
        {
            "user": user,
            "dia": dia,
            "titulo_dia": svc.formatar_dia(dia),
            "hoje": hoje,
            "amanha": hoje + timedelta(days=1),
            "reunioes": svc.reunioes_do_dia(db, dia),
            "notificar": svc.telefone_notificacoes(db),
            "local": _local,
            "erro": erro,
            "ok": ok,
        },
    )


@router.post("/agenda/dia/enviar")
async def enviar_resumo(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    dia = _dia(form.get("data"))
    try:
        quantidade = svc.enviar_resumo(db, dia)
    except (AgendaError, EvolutionError) as exc:
        db.rollback()
        mensagem = str(exc) if isinstance(exc, AgendaError) else "A Evolution não conseguiu enviar. Tente de novo."
        return RedirectResponse(f"/agenda/dia?data={dia.isoformat()}&erro={quote(mensagem)}", status_code=303)
    db.commit()
    mensagem = f"Resumo com {quantidade} reunião(ões) enviado para o WhatsApp de notificações."
    return RedirectResponse(f"/agenda/dia?data={dia.isoformat()}&ok={quote(mensagem)}", status_code=303)


@router.post("/reunioes/{reuniao_id}/observacoes")
async def editar_observacoes(reuniao_id: int, request: Request, db: Session = Depends(get_db)):
    reuniao = db.get(Reuniao, reuniao_id)
    if reuniao is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    reuniao.observacoes = form.get("observacoes", "").strip()[:1000]
    db.commit()
    dia = como_utc(reuniao.inicio).astimezone(svc.regras_padrao().tz).date()
    voltar = form.get("voltar") or f"/agenda/dia?data={dia.isoformat()}"
    if not voltar.startswith("/agenda"):  # só volta para telas da agenda
        voltar = "/agenda"
    return RedirectResponse(voltar, status_code=303)


_ACOES = {
    "cancelar": ReuniaoStatus.CANCELADA,
    "realizada": ReuniaoStatus.REALIZADA,
    "nao-compareceu": ReuniaoStatus.NAO_COMPARECEU,
}


@router.post("/reunioes/{reuniao_id}/{acao}")
def acao_reuniao(reuniao_id: int, acao: str, db: Session = Depends(get_db)):
    reuniao = db.get(Reuniao, reuniao_id)
    if reuniao is None or acao not in _ACOES:
        raise HTTPException(status_code=404)
    try:
        svc.mudar_status(db, reuniao, _ACOES[acao])
    except AgendaError as exc:
        db.rollback()
        return RedirectResponse(f"/agenda?erro={quote(str(exc))}", status_code=303)
    db.commit()
    if reuniao.status == ReuniaoStatus.CANCELADA:
        avisar_cancelamento.delay(reuniao.id)
    return RedirectResponse("/agenda", status_code=303)
