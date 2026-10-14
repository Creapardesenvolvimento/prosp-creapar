from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Time, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ReuniaoStatus(StrEnum):
    AGENDADA = "agendada"
    CANCELADA = "cancelada"
    REALIZADA = "realizada"
    NAO_COMPARECEU = "nao_compareceu"


class Vendedor(Base):
    __tablename__ = "vendedores"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    telefone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    link_reuniao: Mapped[str | None] = mapped_column(String(500))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    horarios: Mapped[list["VendedorHorario"]] = relationship(
        back_populates="vendedor", cascade="all, delete-orphan", order_by="(VendedorHorario.dia_semana, VendedorHorario.inicio)"
    )
    bloqueios: Mapped[list["VendedorBloqueio"]] = relationship(
        back_populates="vendedor", cascade="all, delete-orphan", order_by="VendedorBloqueio.inicio"
    )


class VendedorHorario(Base):
    """Faixa semanal em que o vendedor atende (ex.: segunda, 09:00 às 12:00)."""

    __tablename__ = "vendedor_horarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendedor_id: Mapped[int] = mapped_column(ForeignKey("vendedores.id", ondelete="CASCADE"), index=True)
    dia_semana: Mapped[int] = mapped_column(Integer)  # 0 = segunda ... 6 = domingo
    inicio: Mapped[time] = mapped_column(Time)
    fim: Mapped[time] = mapped_column(Time)

    vendedor: Mapped[Vendedor] = relationship(back_populates="horarios")


class VendedorBloqueio(Base):
    """Período em que o vendedor não atende (folga, férias, compromisso)."""

    __tablename__ = "vendedor_bloqueios"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendedor_id: Mapped[int] = mapped_column(ForeignKey("vendedores.id", ondelete="CASCADE"), index=True)
    inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fim: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    motivo: Mapped[str] = mapped_column(String(200), default="")

    vendedor: Mapped[Vendedor] = relationship(back_populates="bloqueios")


class Reuniao(Base):
    __tablename__ = "reunioes"
    __table_args__ = (
        # Rede de segurança contra reserva dupla do mesmo vendedor no mesmo horário.
        Index(
            "uq_reunioes_vendedor_inicio_agendada",
            "vendedor_id",
            "inicio",
            unique=True,
            postgresql_where=text("status = 'agendada'"),
            sqlite_where=text("status = 'agendada'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendedor_id: Mapped[int] = mapped_column(ForeignKey("vendedores.id"), index=True)
    lead_id: Mapped[int | None] = mapped_column(Integer)  # vira FK para leads na Fase 4
    lead_nome: Mapped[str] = mapped_column(String(200))
    lead_telefone: Mapped[str | None] = mapped_column(String(20))
    inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fim: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=ReuniaoStatus.AGENDADA)
    link: Mapped[str | None] = mapped_column(String(500))
    observacoes: Mapped[str] = mapped_column(Text, default="")
    vendedor_avisado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    vendedor: Mapped[Vendedor] = relationship()
