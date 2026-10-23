from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func, true
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db import Base


class Cliente(Base):
    """Cada cliente da agência tem os próprios números, campanhas, empresas, leads, agenda e configurações."""

    __tablename__ = "clientes"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), unique=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())  # arquivado = False
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    arquivado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DoCliente:
    """Mixin dos modelos que pertencem a um cliente.

    O `cliente_id` é carimbado sozinho ao criar o registro e toda consulta ORM é filtrada pelo cliente
    da sessão (ver app.tenancy). Excluir o cliente apaga tudo dele.
    """

    @declared_attr
    def cliente_id(cls) -> Mapped[int]:
        return mapped_column(Integer, ForeignKey("clientes.id", ondelete="CASCADE"), index=True)
