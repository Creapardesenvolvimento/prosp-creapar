from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WebhookEvent(Base):
    """Registro de eventos já processados, para ignorar reenvios."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("origem", "chave", name="uq_webhook_events_origem_chave"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    origem: Mapped[str] = mapped_column(String(40))
    chave: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
