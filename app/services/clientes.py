"""Cadastro dos clientes da agência: criar (copiando configurações de outro, se quiser), renomear,
arquivar, reativar e excluir de vez.

As funções alteram a sessão mas não fazem commit.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.integrations.evolution import EvolutionError
from app.models import (
    Blacklist,
    Busca,
    Campanha,
    CampanhaFollowup,
    CampanhaMensagem,
    CampanhaStatus,
    Cliente,
    Configuracao,
    Empresa,
    Envio,
    EnvioStatus,
    EnvioTipo,
    Lead,
    LeadStatus,
    Mensagem,
    NumeroStatus,
    PlanoBusca,
    Reuniao,
    ReuniaoStatus,
    Roteiro,
    Segmento,
    Vendedor,
    VendedorBloqueio,
    VendedorHorario,
    WhatsappNumber,
)
from app.models.campanhas import campanha_numeros
from app.tenancy import TODOS, no_cliente

logger = logging.getLogger(__name__)

# Configurações que dizem quem o cliente é: nunca vêm copiadas de outro cliente.
IDENTIDADE = ("empresa_nome", "atendente_nome", "economia_texto", "notificar_telefone")


class ClienteError(Exception):
    pass


def _nome_livre(db: Session, nome: str, exceto: int | None = None) -> str:
    nome = nome.strip()[:120]
    if not nome:
        raise ClienteError("Informe o nome do cliente.")
    repetido = db.scalar(select(Cliente.id).where(func.lower(Cliente.nome) == nome.lower(), Cliente.id != (exceto or 0)))
    if repetido:
        raise ClienteError("Já existe um cliente com esse nome.")
    return nome


def criar(
    db: Session,
    *,
    nome: str,
    empresa_nome: str = "",
    atendente_nome: str = "",
    economia_texto: str = "",
    copiar_de: Cliente | None = None,
    copiar_roteiros: bool = False,
) -> Cliente:
    """Cria o cliente. Copiando de outro, vêm as configurações (horários, limites, oferta…) e, se pedir, os roteiros."""
    from app.services import config_sistema

    cliente = Cliente(nome=_nome_livre(db, nome), ativo=True)
    db.add(cliente)
    db.flush()
    origem = []
    if copiar_de is not None:
        with no_cliente(db, copiar_de.id):
            origem = [(c.chave, c.valor) for c in db.scalars(select(Configuracao)) if c.chave not in IDENTIDADE]
            roteiros = [
                (s.nome, s.palavras_chave, [(r.porte, r.foco, r.etapa, r.texto) for r in s.roteiros])
                for s in db.scalars(select(Segmento))
            ] if copiar_roteiros else []
    with no_cliente(db, cliente.id):
        for chave, valor in origem:
            db.add(Configuracao(chave=chave, valor=valor))
        db.flush()
        identidade = {
            "empresa_nome": empresa_nome.strip() or cliente.nome,
            "atendente_nome": atendente_nome.strip() or config_sistema.padroes()["atendente_nome"],
            "economia_texto": economia_texto.strip() or config_sistema.padroes()["economia_texto"],
        }
        if copiar_de is None:
            # A oferta e as instruções de fábrica são as da Brafort10: um cliente novo começa em branco.
            identidade.update(campanha_oferta="", campanha_instrucoes="")
        try:
            config_sistema.salvar(db, identidade)
        except config_sistema.ConfigError as exc:
            raise ClienteError(str(exc)) from exc
        if copiar_de is not None and copiar_roteiros:
            for nome_seg, palavras, itens in roteiros:
                segmento = Segmento(nome=nome_seg, palavras_chave=palavras)
                db.add(segmento)
                db.flush()
                for porte, foco, etapa, texto in itens:
                    db.add(Roteiro(segmento_id=segmento.id, porte=porte, foco=foco, etapa=etapa, texto=texto))
        db.flush()
    logger.info("cliente criado", extra={"cliente_id": cliente.id, "copiou_de": copiar_de.id if copiar_de else None})
    return cliente


def renomear(db: Session, cliente: Cliente, nome: str) -> None:
    cliente.nome = _nome_livre(db, nome, exceto=cliente.id)


def arquivar(db: Session, cliente: Cliente) -> None:
    """Some do seletor e para todos os disparos e tarefas. Os dados ficam; dá para reativar."""
    if not cliente.ativo:
        raise ClienteError("O cliente já está arquivado.")
    cliente.ativo = False
    cliente.arquivado_em = datetime.now(timezone.utc)
    with no_cliente(db, cliente.id):
        db.execute(
            update(Campanha).where(Campanha.status == CampanhaStatus.ATIVA).values(status=CampanhaStatus.PAUSADA)
            .execution_options(synchronize_session=False)
        )
    logger.info("cliente arquivado", extra={"cliente_id": cliente.id})


def reativar(db: Session, cliente: Cliente) -> None:
    if cliente.ativo:
        raise ClienteError("O cliente já está ativo.")
    cliente.ativo, cliente.arquivado_em = True, None
    logger.info("cliente reativado", extra={"cliente_id": cliente.id})


def excluir(db: Session, cliente: Cliente, confirmacao: str) -> int:
    """Apaga o cliente e tudo dele. Desconecta e apaga os números na Evolution. Retorna quantos números saíram."""
    from app.services import numeros

    if confirmacao.strip().lower() != cliente.nome.strip().lower():
        raise ClienteError("Para excluir, digite o nome do cliente exatamente como aparece.")
    removidos = 0
    with no_cliente(db, cliente.id):
        for numero in db.scalars(select(WhatsappNumber).where(WhatsappNumber.status != NumeroStatus.REMOVIDO)).all():
            try:
                numeros.remover(db, numero)
                removidos += 1
            except EvolutionError as exc:  # a instância fica órfã na Evolution, mas o cliente sai do painel
                logger.warning("não consegui remover o número na Evolution", extra={"numero_id": numero.id, "erro": str(exc)})
        cid = cliente.id
        campanhas = select(Campanha.id).where(Campanha.cliente_id == cid)
        vendedores = select(Vendedor.id).where(Vendedor.cliente_id == cid)
        segmentos = select(Segmento.id).where(Segmento.cliente_id == cid)
        db.flush()
        # Dos dependentes para os donos (as tabelas filhas sem cliente saem pelo dono). O filtro do cliente
        # vai escrito em cada comando, além do automático: um DELETE sem filtro apagaria todos os clientes.
        for comando in (
            delete(Mensagem).where(Mensagem.cliente_id == cid),
            delete(Envio).where(Envio.cliente_id == cid),
            delete(Reuniao).where(Reuniao.cliente_id == cid),
            delete(VendedorHorario).where(VendedorHorario.vendedor_id.in_(vendedores)),
            delete(VendedorBloqueio).where(VendedorBloqueio.vendedor_id.in_(vendedores)),
            delete(Vendedor).where(Vendedor.cliente_id == cid),
            delete(Empresa).where(Empresa.cliente_id == cid),
            delete(PlanoBusca).where(PlanoBusca.cliente_id == cid),
            delete(Lead).where(Lead.cliente_id == cid),
            delete(Busca).where(Busca.cliente_id == cid),
            delete(CampanhaFollowup).where(CampanhaFollowup.campanha_id.in_(campanhas)),
            delete(CampanhaMensagem).where(CampanhaMensagem.campanha_id.in_(campanhas)),
            campanha_numeros.delete().where(campanha_numeros.c.campanha_id.in_(campanhas)),
            delete(Campanha).where(Campanha.cliente_id == cid),
            delete(Roteiro).where(Roteiro.segmento_id.in_(segmentos)),
            delete(Segmento).where(Segmento.cliente_id == cid),
            delete(Blacklist).where(Blacklist.cliente_id == cid),
            delete(Configuracao).where(Configuracao.cliente_id == cid),
            delete(WhatsappNumber).where(WhatsappNumber.cliente_id == cid),
        ):
            db.execute(comando.execution_options(synchronize_session=False))
    db.execute(delete(Cliente).where(Cliente.id == cliente.id).execution_options(synchronize_session=False))
    db.expunge_all()
    logger.warning("cliente excluído", extra={"cliente_id": cliente.id, "numeros_removidos": removidos})
    return removidos


def resumo(db: Session) -> list[dict]:
    """Uma linha por cliente com o que importa para escolher onde trabalhar."""
    desde = datetime.now(timezone.utc) - timedelta(hours=24)

    def por_cliente(consulta) -> dict[int, int]:
        return dict(db.execute(consulta.execution_options(**TODOS)).all())

    numeros = por_cliente(
        select(WhatsappNumber.cliente_id, func.count()).where(WhatsappNumber.status == NumeroStatus.CONECTADO).group_by(WhatsappNumber.cliente_id)
    )
    campanhas = por_cliente(
        select(Campanha.cliente_id, func.count()).where(Campanha.status == CampanhaStatus.ATIVA).group_by(Campanha.cliente_id)
    )
    leads = por_cliente(select(Lead.cliente_id, func.count()).group_by(Lead.cliente_id))
    na_fila = por_cliente(select(Lead.cliente_id, func.count()).where(Lead.status == LeadStatus.NA_FILA).group_by(Lead.cliente_id))
    envios = por_cliente(
        select(Envio.cliente_id, func.count())
        .where(Envio.tipo == EnvioTipo.ABERTURA, Envio.status == EnvioStatus.ENVIADO, Envio.enviado_em >= desde)
        .group_by(Envio.cliente_id)
    )
    respostas = por_cliente(select(Lead.cliente_id, func.count()).where(Lead.respondeu_em >= desde).group_by(Lead.cliente_id))
    reunioes = por_cliente(
        select(Reuniao.cliente_id, func.count())
        .where(Reuniao.status == ReuniaoStatus.AGENDADA, Reuniao.inicio >= datetime.now(timezone.utc))
        .group_by(Reuniao.cliente_id)
    )
    return [
        {
            "cliente": c,
            "numeros": numeros.get(c.id, 0),
            "campanhas": campanhas.get(c.id, 0),
            "leads": leads.get(c.id, 0),
            "na_fila": na_fila.get(c.id, 0),
            "envios_24h": envios.get(c.id, 0),
            "respostas_24h": respostas.get(c.id, 0),
            "reunioes": reunioes.get(c.id, 0),
        }
        for c in db.scalars(select(Cliente).order_by(Cliente.ativo.desc(), Cliente.nome))
    ]
