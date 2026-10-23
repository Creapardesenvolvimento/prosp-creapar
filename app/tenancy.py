"""Separação por cliente.

A sessão do banco carrega o cliente em `session.info["cliente_id"]`:
- toda consulta ORM (SELECT, UPDATE, DELETE, inclusive subconsultas e relacionamentos) recebe o filtro
  `cliente_id = <cliente da sessão>` nos modelos `DoCliente`;
- todo registro novo desses modelos é carimbado com o cliente da sessão.

Sessão sem cliente não filtra: é a visão do sistema (tarefas que percorrem todos os clientes, webhooks antes
de saber de quem é o número). Telas sempre definem o cliente (ver app.api.deps.current_user).

Cuidado: `exists().where(...)` do SQLAlchemy Core escapa do filtro. Use `select(...).exists()`.
"""

from contextlib import contextmanager

from sqlalchemy import event, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.models.cliente import Cliente, DoCliente

TODOS = {"todos_os_clientes": True}  # execution_options para consultar sem o filtro


class SemCliente(RuntimeError):
    pass


def usar_cliente(db: Session, cliente_id: int | None) -> None:
    db.info["cliente_id"] = cliente_id


def cliente_id(db: Session) -> int | None:
    return db.info.get("cliente_id")


def exigir_cliente(db: Session) -> int:
    cid = cliente_id(db)
    if cid is None:
        raise SemCliente("operação que depende do cliente rodou numa sessão sem cliente")
    return cid


@contextmanager
def no_cliente(db: Session, cliente_id_: int):
    anterior = db.info.get("cliente_id")
    db.info["cliente_id"] = cliente_id_
    try:
        yield
    finally:
        db.info["cliente_id"] = anterior


def clientes_ativos(db: Session) -> list[int]:
    return list(db.scalars(select(Cliente.id).where(Cliente.ativo).order_by(Cliente.id)))


@event.listens_for(Session, "do_orm_execute")
def _filtrar_por_cliente(state: ORMExecuteState) -> None:
    cid = state.session.info.get("cliente_id")
    if cid is None or state.execution_options.get("todos_os_clientes"):
        return
    if state.is_select or state.is_update or state.is_delete:
        state.statement = state.statement.options(
            with_loader_criteria(DoCliente, lambda cls: cls.cliente_id == cid, include_aliases=True)
        )


@event.listens_for(Session, "transient_to_pending")
def _carimbar_cliente(session: Session, obj) -> None:
    # Carimba ao adicionar (não ao gravar): uma tarefa que troca de cliente no meio não mistura registros.
    if isinstance(obj, DoCliente) and obj.cliente_id is None:
        cid = session.info.get("cliente_id")
        if cid is None:
            raise SemCliente(f"{type(obj).__name__} criado numa sessão sem cliente")
        obj.cliente_id = cid
