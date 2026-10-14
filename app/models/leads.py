from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class BuscaStatus(StrEnum):
    PENDENTE = "pendente"
    EXECUTANDO = "executando"  # actor rodando no Apify
    PROCESSANDO = "processando"  # filtrando e validando WhatsApp
    CONCLUIDA = "concluida"
    ERRO = "erro"


class LeadStatus(StrEnum):
    NOVO = "novo"
    NA_FILA = "na_fila"
    ENVIADO = "enviado"
    RESPONDEU = "respondeu"
    EM_CONVERSA = "em_conversa"
    REUNIAO_AGENDADA = "reuniao_agendada"
    SEM_INTERESSE = "sem_interesse"
    HUMANO_ASSUMIU = "humano_assumiu"
    OPT_OUT = "opt_out"
    SEM_RESPOSTA = "sem_resposta"


class Busca(Base):
    __tablename__ = "buscas"

    id: Mapped[int] = mapped_column(primary_key=True)
    termo: Mapped[str] = mapped_column(String(200))
    cidade: Mapped[str] = mapped_column(String(200))
    limite: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=BuscaStatus.PENDENTE, index=True)
    apify_run_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    apify_dataset_id: Mapped[str | None] = mapped_column(String(64))
    total_encontrados: Mapped[int] = mapped_column(Integer, default=0)
    total_novos: Mapped[int] = mapped_column(Integer, default=0)
    descartes: Mapped[dict] = mapped_column(JSON, default=dict)
    erro: Mapped[str | None] = mapped_column(Text)
    criado_por_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    busca_id: Mapped[int | None] = mapped_column(ForeignKey("buscas.id"), index=True)
    nome: Mapped[str] = mapped_column(String(300))
    telefone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164
    whatsapp_jid: Mapped[str | None] = mapped_column(String(64))
    site: Mapped[str | None] = mapped_column(String(500))
    categoria: Mapped[str | None] = mapped_column(String(200))
    endereco: Mapped[str | None] = mapped_column(String(500))
    cidade: Mapped[str | None] = mapped_column(String(200))
    nota: Mapped[float | None] = mapped_column(Float)
    avaliacoes: Mapped[int | None] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(String(255))
    place_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=LeadStatus.NOVO, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    busca: Mapped[Busca | None] = relationship()


class Blacklist(Base):
    """Telefones que nunca mais recebem mensagem (opt-out ou bloqueio manual)."""

    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    telefone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164
    motivo: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
