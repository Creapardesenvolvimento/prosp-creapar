"""Base geral de empresas: tudo o que entrou por busca ou lista importada, sem repetir, com a situação na prospecção.

Uma empresa aparece uma vez por telefone (ou, sem telefone, pelo id do Google Maps): vale o registro mais
recente, que aponta para o lead quando ele existe. Filtrando por uma lista, aparecem as empresas daquela lista.
"""

from dataclasses import dataclass

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models import Campanha, Empresa, Lead, LeadStatus
from app.services import situacao as situacao_svc

POR_PAGINA = 100


@dataclass
class Filtros:
    situacao: str = ""
    texto: str = ""
    lista: int | None = None


def _unicas():
    chave = func.coalesce(Empresa.telefone, Empresa.place_id, cast(Empresa.id, String))
    return select(func.max(Empresa.id)).group_by(chave)


def _base(filtros: Filtros, com_situacao: bool = True):
    consulta = select(Empresa, Lead).outerjoin(Lead, Lead.id == Empresa.lead_id)
    if filtros.lista:
        consulta = consulta.where(Empresa.busca_id == filtros.lista)
    else:
        consulta = consulta.where(Empresa.id.in_(_unicas()))
    texto = filtros.texto.strip()
    if texto:
        digitos = "".join(c for c in texto if c.isdigit())
        opcoes = [Empresa.nome.ilike(f"%{texto}%"), Empresa.cidade.ilike(f"%{texto}%"), Empresa.categoria.ilike(f"%{texto}%")]
        if len(digitos) >= 4:
            opcoes.append(Empresa.telefone.like(f"%{digitos}%"))
        consulta = consulta.where(or_(*opcoes))
    if com_situacao and filtros.situacao in situacao_svc.POR_CHAVE:
        consulta = consulta.where(situacao_svc.condicao(filtros.situacao))
    return consulta


def listar(db: Session, filtros: Filtros, pagina: int = 1) -> tuple[list[tuple], int]:
    """([(empresa, lead, situação)], total)."""
    consulta = _base(filtros)
    total = db.scalar(select(func.count()).select_from(consulta.subquery())) or 0
    linhas = db.execute(
        consulta.order_by(Empresa.id.desc()).offset((max(pagina, 1) - 1) * POR_PAGINA).limit(POR_PAGINA)
    ).all()
    return [(e, l, situacao_svc.situacao(l, e)) for e, l in linhas], total


def todas(db: Session, filtros: Filtros) -> list[tuple]:
    return [(e, l, situacao_svc.situacao(l, e)) for e, l in db.execute(_base(filtros).order_by(Empresa.id)).all()]


def contagens(db: Session, filtros: Filtros) -> dict[str, int]:
    """Situação -> quantidade, com os mesmos filtros de texto e lista (sem o de situação)."""
    sub = _base(filtros, com_situacao=False).subquery()
    linhas = db.execute(
        select(sub.c.status, sub.c.followups_enviados > 0, sub.c.descarte.is_not(None), func.count())
        .group_by(sub.c.status, sub.c.followups_enviados > 0, sub.c.descarte.is_not(None))
    ).all()
    resultado: dict[str, int] = {}
    for status, tem_followup, descartada, quantidade in linhas:
        chave = situacao_svc.chave_da_situacao(status, 1 if tem_followup else 0, bool(descartada))
        resultado[chave] = resultado.get(chave, 0) + quantidade
    return resultado


def leads_nao_contatados(db: Session, filtros: Filtros) -> list[int]:
    filtros = Filtros(situacao="nao_contatado", texto=filtros.texto, lista=filtros.lista)
    return [l.id for _, l in db.execute(_base(filtros)).all() if l is not None and l.status == LeadStatus.NOVO and l.campanha_id is None]


def campanhas_dos_leads(db: Session, leads: list[Lead]) -> dict[int, Campanha]:
    ids = {l.campanha_id for l in leads if l is not None and l.campanha_id}
    return {c.id: c for c in db.scalars(select(Campanha).where(Campanha.id.in_(ids)))} if ids else {}
