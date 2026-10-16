from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.whatsapp import WhatsappNumber


class CampanhaStatus(StrEnum):
    RASCUNHO = "rascunho"
    ATIVA = "ativa"
    PAUSADA = "pausada"
    ENCERRADA = "encerrada"


class EnvioTipo(StrEnum):
    ABERTURA = "abertura"
    FOLLOW_UP = "follow_up"
    IA = "ia"


class EnvioStatus(StrEnum):
    PENDENTE = "pendente"
    ENVIADO = "enviado"
    FALHOU = "falhou"


campanha_numeros = Table(
    "campanha_numeros",
    Base.metadata,
    Column("campanha_id", ForeignKey("campanhas.id", ondelete="CASCADE"), primary_key=True),
    Column("numero_id", ForeignKey("whatsapp_numbers.id", ondelete="CASCADE"), primary_key=True),
)


class Campanha(Base):
    __tablename__ = "campanhas"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(200))
    nicho: Mapped[str] = mapped_column(String(200), default="")
    oferta: Mapped[str] = mapped_column(Text, default="")
    prompt_agente: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=CampanhaStatus.RASCUNHO, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    mensagens: Mapped[list["CampanhaMensagem"]] = relationship(
        back_populates="campanha", cascade="all, delete-orphan", order_by="CampanhaMensagem.id"
    )
    numeros: Mapped[list[WhatsappNumber]] = relationship(secondary=campanha_numeros, order_by=WhatsappNumber.id)
    followups: Mapped[list["CampanhaFollowup"]] = relationship(
        cascade="all, delete-orphan", order_by="(CampanhaFollowup.passo, CampanhaFollowup.id)"
    )


class CampanhaMensagem(Base):
    """Variação da mensagem de abertura. Aceita {empresa} e {cidade}."""

    __tablename__ = "campanha_mensagens"

    id: Mapped[int] = mapped_column(primary_key=True)
    campanha_id: Mapped[int] = mapped_column(ForeignKey("campanhas.id", ondelete="CASCADE"), index=True)
    texto: Mapped[str] = mapped_column(Text)
    origem: Mapped[str] = mapped_column(String(20), default="manual")  # manual | ia
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campanha: Mapped[Campanha] = relationship(back_populates="mensagens")


class CampanhaFollowup(Base):
    """Variação de texto de um passo do follow-up (1 = primeiro). Sem variação, valem os textos de fábrica."""

    __tablename__ = "campanha_followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    campanha_id: Mapped[int] = mapped_column(ForeignKey("campanhas.id", ondelete="CASCADE"), index=True)
    passo: Mapped[int] = mapped_column(Integer)
    texto: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Envio(Base):
    """Mensagem que o sistema mandou. O id da Evolution separa o que foi o sistema do que foi um humano."""

    __tablename__ = "envios"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    campanha_id: Mapped[int | None] = mapped_column(ForeignKey("campanhas.id"), index=True)
    numero_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_numbers.id"), index=True)
    mensagem_id: Mapped[int | None] = mapped_column(ForeignKey("campanha_mensagens.id"))
    tipo: Mapped[str] = mapped_column(String(20), default=EnvioTipo.ABERTURA)
    texto: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=EnvioStatus.PENDENTE)
    evolution_message_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    erro: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    enviado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
