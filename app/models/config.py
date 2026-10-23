from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.cliente import DoCliente


class Configuracao(DoCliente, Base):
    """Parâmetro ajustável pelo painel, por cliente. Sem linha aqui, vale o padrão de fábrica."""

    __tablename__ = "configuracoes"
    __table_args__ = (UniqueConstraint("cliente_id", "chave", name="uq_configuracoes_cliente_chave"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(60))
    valor: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
