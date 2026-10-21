from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, templates
from app.config import get_settings
from app.db import get_db
from app.models import Campanha, CampanhaStatus, PlanoBusca, User
from app.services import abastecimento, config_sistema, prospeccao
from app.services.abastecimento import AbastecimentoError
from app.services.config_sistema import ConfigError
from app.services.prospeccao import ProspeccaoError
from app.services.numeros import virada_do_dia as recalcular_limites

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/prospeccao")
def pagina(
    request: Request,
    erro: str | None = None,
    ok: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    settings = get_settings()
    campanhas = db.scalars(
        select(Campanha).where(Campanha.status != CampanhaStatus.ENCERRADA).order_by(Campanha.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "prospeccao/pagina.html",
        {
            "user": user,
            "p": prospeccao.painel(db),
            "ab": abastecimento.situacao(db),
            "campanhas": campanhas,
            "limite_maximo": settings.busca_limite_maximo,
            "preco": settings.apify_preco_por_lugar,
            "apify_configurado": bool(settings.apify_token),
            "erro": erro,
            "ok": ok,
        },
    )


def _voltar(erro: str | None = None, ok: str | None = None) -> RedirectResponse:
    if erro:
        return RedirectResponse(f"/prospeccao?erro={quote(erro)}#abastecimento", status_code=303)
    return RedirectResponse(f"/prospeccao?ok={quote(ok or '')}#abastecimento", status_code=303)


@router.post("/prospeccao/abastecimento")
def salvar_abastecimento(
    abastecer_ativo: str = Form(""),
    abastecer_estoque_dias: str = Form(""),
    abastecer_gasto_diario: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        config_sistema.salvar(
            db,
            {
                "abastecer_ativo": abastecer_ativo,
                "abastecer_estoque_dias": abastecer_estoque_dias,
                "abastecer_gasto_diario": abastecer_gasto_diario,
            },
        )
    except ConfigError as exc:
        db.rollback()
        return _voltar(erro=str(exc))
    db.commit()
    return _voltar(ok="Abastecimento salvo.")


@router.post("/prospeccao/plano")
def adicionar_plano(
    termo: str = Form(""),
    cidades: str = Form(""),
    limite: int = Form(100),
    campanha_id: str = Form(""),
    db: Session = Depends(get_db),
):
    campanha = db.get(Campanha, int(campanha_id)) if campanha_id.isdigit() else None
    try:
        novos = abastecimento.adicionar_ao_plano(db, termo=termo, cidades=cidades, limite=limite, campanha=campanha)
    except AbastecimentoError as exc:
        db.rollback()
        return _voltar(erro=str(exc))
    db.commit()
    return _voltar(ok=f"{novos} busca(s) entraram no plano." if novos else "Essas buscas já estavam no plano.")


@router.post("/prospeccao/plano/{item_id}/remover")
def remover_plano(item_id: int, db: Session = Depends(get_db)):
    item = db.get(PlanoBusca, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    abastecimento.remover_do_plano(db, item)
    db.commit()
    return _voltar(ok="Removida do plano.")


@router.post("/prospeccao/abastecer-agora")
def abastecer_agora(db: Session = Depends(get_db)):
    from app.tasks.buscas import iniciar_busca

    busca = abastecimento.abastecer(db)
    db.commit()
    if busca is None:
        return _voltar(
            erro="Nada a buscar agora: abastecimento desligado, fila acima do alvo, busca em andamento, "
            "teto de gasto do dia atingido ou plano sem buscas pendentes."
        )
    iniciar_busca.delay(busca.id)
    return _voltar(ok=f"Busca iniciada: {busca.termo} — {busca.cidade}.")


@router.post("/prospeccao/puxar-numeros")
def puxar_numeros(db: Session = Depends(get_db)):
    """Confere a conexão de todos os números na Evolution e põe os disponíveis nas campanhas ativas."""
    from app.services.numeros import conferir_conexoes

    conferir_conexoes(db)  # atualiza conectado/desconectado antes de puxar (faz commit por número)
    try:
        campanhas, novos = prospeccao.puxar_numeros(db)
    except ProspeccaoError as exc:
        db.rollback()
        return RedirectResponse(f"/prospeccao?erro={quote(str(exc))}", status_code=303)
    recalcular_limites(db)
    db.commit()
    texto = f"{novos} número(s) entraram em {campanhas} campanha(s) ativa(s)." if novos else "Todos os números já estavam nas campanhas ativas."
    return RedirectResponse(f"/prospeccao?ok={quote(texto + ' Conexões atualizadas.')}", status_code=303)


@router.get("/prospeccao/painel")
def painel(request: Request, db: Session = Depends(get_db)):
    """Parcial que o HTMX recarrega a cada 30 segundos."""
    return templates.TemplateResponse(request, "prospeccao/_painel.html", {"p": prospeccao.painel(db)})


@router.post("/prospeccao/metas")
def salvar_metas(
    meta_diaria: str = Form(""),
    teto_por_numero: str = Form(""),
    limite_hora_numero: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        config_sistema.salvar(
            db,
            {"meta_diaria": meta_diaria, "teto_por_numero": teto_por_numero, "limite_hora_numero": limite_hora_numero},
        )
    except ConfigError as exc:
        db.rollback()
        return RedirectResponse(f"/prospeccao?erro={quote(str(exc))}", status_code=303)
    recalcular_limites(db)  # o teto novo vale já hoje
    db.commit()
    return RedirectResponse(f"/prospeccao?ok={quote('Metas salvas.')}", status_code=303)
