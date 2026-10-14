from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Direcao(StrEnum):
    ENTRADA = "entrada"
    SAIDA = "saida"


class Autor(StrEnum):
    LEAD = "lead"
    SISTEMA = "sistema"  # mensagem de abertura / follow-up do disparo
    IA = "ia"
    HUMANO = "humano"  # alguém do time respondeu pelo Chatwoot ou pelo celular


class Mensagem(Base):
    """Histórico da conversa com o lead (o que entrou e o que saiu)."""

    __tablename__ = "mensagens"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    numero_id: Mapped[int | None] = mapped_column(ForeignKey("whatsapp_numbers.id"))
    direcao: Mapped[str] = mapped_column(String(10))
    autor: Mapped[str] = mapped_column(String(10))
    tipo: Mapped[str] = mapped_column(String(30), default="texto")  # texto, audio, imagem…
    texto: Mapped[str] = mapped_column(Text, default="")
    evolution_message_id: Mapped[str | None] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
