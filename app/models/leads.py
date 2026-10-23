from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.cliente import DoCliente


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
    DESQUALIFICADO = "desqualificado"  # não serve para a oferta (não é empresa, número errado, fora do perfil)
    JA_PROSPECTADO = "ja_prospectado"  # tirado da fila: ele (ou o mesmo WhatsApp em outro cadastro) já foi abordado


class Busca(DoCliente, Base):
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
    origem: Mapped[str] = mapped_column(String(20), default="apify", server_default="apify")  # apify | importacao
    # Campanha que recebe os leads assim que a lista termina de ser processada (importação).
    campanha_id: Mapped[int | None] = mapped_column(ForeignKey("campanhas.id", ondelete="SET NULL"))

    @property
    def importada(self) -> bool:
        return self.origem == "importacao"

    @property
    def rotulo(self) -> str:
        return f"Lista: {self.termo}" if self.importada else f"{self.termo} — {self.cidade}"


class Lead(DoCliente, Base):
    __tablename__ = "leads"
    # Cada cliente é independente: a mesma empresa pode ser lead de mais de um cliente.
    __table_args__ = (UniqueConstraint("cliente_id", "telefone", name="uq_leads_cliente_telefone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    busca_id: Mapped[int | None] = mapped_column(ForeignKey("buscas.id"), index=True)
    nome: Mapped[str] = mapped_column(String(300))
    telefone: Mapped[str] = mapped_column(String(20))  # E.164
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
    campanha_id: Mapped[int | None] = mapped_column(ForeignKey("campanhas.id"), index=True)
    # Número que mandou a primeira mensagem. Depois de preenchido, só ele fala com o lead.
    numero_id: Mapped[int | None] = mapped_column(ForeignKey("whatsapp_numbers.id"), index=True)
    tentativas_envio: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enviado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    respondeu_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followups_enviados: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ultimo_followup_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Agente de IA
    segmento: Mapped[str | None] = mapped_column(String(120))
    porte: Mapped[str | None] = mapped_column(String(10))
    etapa: Mapped[str | None] = mapped_column(String(20))  # próxima etapa do roteiro a entregar
    ia_ativa: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    handoff_motivo: Mapped[str | None] = mapped_column(String(300))
    # Quando a IA deve responder (empurrado a cada mensagem nova, para juntar mensagens seguidas).
    responder_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    busca: Mapped[Busca | None] = relationship()


class PlanoBusca(DoCliente, Base):
    """Termo × cidade na fila do abastecimento automático: vira busca no Apify quando a fila de leads baixa."""

    __tablename__ = "plano_buscas"

    id: Mapped[int] = mapped_column(primary_key=True)
    termo: Mapped[str] = mapped_column(String(200))
    cidade: Mapped[str] = mapped_column(String(200))
    limite: Mapped[int] = mapped_column(Integer)
    campanha_id: Mapped[int | None] = mapped_column(ForeignKey("campanhas.id", ondelete="SET NULL"))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    busca_id: Mapped[int | None] = mapped_column(ForeignKey("buscas.id", ondelete="SET NULL"))  # a última feita
    buscado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    busca: Mapped[Busca | None] = relationship()


class Blacklist(DoCliente, Base):
    """Telefones que nunca mais recebem mensagem deste cliente (opt-out ou bloqueio manual)."""

    __tablename__ = "blacklist"
    __table_args__ = (UniqueConstraint("cliente_id", "telefone", name="uq_blacklist_cliente_telefone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telefone: Mapped[str] = mapped_column(String(20))  # E.164
    motivo: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
