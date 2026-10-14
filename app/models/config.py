from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Configuracao(Base):
    """Parâmetro ajustável pelo painel. Sem linha aqui, vale o padrão de fábrica."""

    __tablename__ = "configuracoes"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
