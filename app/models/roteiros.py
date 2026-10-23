from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.cliente import DoCliente


class Porte(StrEnum):
    PEQUENO = "pequeno"
    MEDIO = "medio"
    GRANDE = "grande"


class Etapa(StrEnum):
    """Etapas do roteiro (AIDA). A abertura é a saudação do disparo, antes do roteiro."""

    ATENCAO = "atencao"
    INTERESSE = "interesse"
    DESEJO = "desejo"
    ACAO = "acao"


ETAPAS = [Etapa.ATENCAO, Etapa.INTERESSE, Etapa.DESEJO, Etapa.ACAO]


class Segmento(DoCliente, Base):
    __tablename__ = "segmentos"
    __table_args__ = (UniqueConstraint("cliente_id", "nome", name="uq_segmentos_cliente_nome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    # Termos que identificam o segmento na categoria do Google Maps ou no termo da busca.
    palavras_chave: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    roteiros: Mapped[list["Roteiro"]] = relationship(back_populates="segmento", cascade="all, delete-orphan")


class Roteiro(DoCliente, Base):
    """Texto-base de uma etapa para um segmento e porte."""

    __tablename__ = "roteiros"
    __table_args__ = (UniqueConstraint("segmento_id", "porte", "etapa", name="uq_roteiros_segmento_porte_etapa"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    segmento_id: Mapped[int] = mapped_column(ForeignKey("segmentos.id", ondelete="CASCADE"), index=True)
    porte: Mapped[str] = mapped_column(String(10))
    foco: Mapped[str] = mapped_column(String(200), default="")
    etapa: Mapped[str] = mapped_column(String(20))
    texto: Mapped[str] = mapped_column(Text)

    segmento: Mapped[Segmento] = relationship(back_populates="roteiros")
