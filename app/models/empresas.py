from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.cliente import DoCliente


class Empresa(DoCliente, Base):
    """Cada empresa que o Apify devolveu numa busca, virando lead ou não. Guarda tudo o que foi pago."""

    __tablename__ = "empresas"
    __table_args__ = (UniqueConstraint("busca_id", "place_id", name="uq_empresas_busca_place"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    busca_id: Mapped[int] = mapped_column(ForeignKey("buscas.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[str | None] = mapped_column(String(200), index=True)
    nome: Mapped[str] = mapped_column(String(300))
    telefone_original: Mapped[str | None] = mapped_column(String(50))
    telefone: Mapped[str | None] = mapped_column(String(20), index=True)  # E.164, quando é brasileiro válido
    celular: Mapped[bool] = mapped_column(Boolean, default=False)
    site: Mapped[str | None] = mapped_column(String(500))
    categoria: Mapped[str | None] = mapped_column(String(200))
    endereco: Mapped[str | None] = mapped_column(String(500))
    bairro: Mapped[str | None] = mapped_column(String(200))
    cidade: Mapped[str | None] = mapped_column(String(200))
    estado: Mapped[str | None] = mapped_column(String(100))
    nota: Mapped[float | None] = mapped_column(Float)
    avaliacoes: Mapped[int | None] = mapped_column(Integer)
    url_maps: Mapped[str | None] = mapped_column(String(1000))
    descarte: Mapped[str | None] = mapped_column(String(40))  # motivo; vazio quando virou lead
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    dados: Mapped[dict] = mapped_column(JSON)  # item completo do Apify
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
