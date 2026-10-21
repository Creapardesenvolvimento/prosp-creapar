from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import EncryptedString


class NumeroStatus(StrEnum):
    AGUARDANDO_QR = "aguardando_qr"
    CONECTADO = "conectado"
    DESCONECTADO = "desconectado"
    PAUSADO = "pausado"
    BANIDO = "banido"
    REMOVIDO = "removido"


class EvolutionServer(Base):
    __tablename__ = "evolution_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(EncryptedString)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WhatsappNumber(Base):
    __tablename__ = "whatsapp_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("evolution_servers.id"))
    nome_exibicao: Mapped[str] = mapped_column(String(120))
    instance_name: Mapped[str] = mapped_column(String(120), unique=True)
    telefone: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=NumeroStatus.AGUARDANDO_QR, index=True)
    data_ativacao: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    limite_diario_atual: Mapped[int] = mapped_column(Integer, default=0)
    envios_hoje: Mapped[int] = mapped_column(Integer, default=0)
    chatwoot_inbox_id: Mapped[int | None] = mapped_column(Integer)
    substitui_numero_id: Mapped[int | None] = mapped_column(ForeignKey("whatsapp_numbers.id"))
    # statusReason do último "close" da Evolution (401 = sessão encerrada no aparelho, etc.)
    motivo_desconexao: Mapped[int | None] = mapped_column(Integer)
    # Disparo: quando este número pode mandar a próxima mensagem, e por que foi pausado.
    proximo_envio_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    motivo_pausa: Mapped[str | None] = mapped_column(String(200))
    webhook_versao: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Proxy próprio do número (vazio = sai pelo IP do servidor da Evolution). A senha fica criptografada.
    proxy_protocolo: Mapped[str | None] = mapped_column(String(10))  # http | socks5
    proxy_host: Mapped[str | None] = mapped_column(String(255))
    proxy_porta: Mapped[str | None] = mapped_column(String(10))
    proxy_usuario: Mapped[str | None] = mapped_column(String(255))
    proxy_senha: Mapped[str | None] = mapped_column(EncryptedString)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def tem_proxy(self) -> bool:
        return bool(self.proxy_host)

    server: Mapped[EvolutionServer] = relationship()
    substitui: Mapped["WhatsappNumber | None"] = relationship(remote_side=[id])
