"""agente de IA: segmentos, roteiros, mensagens e campos do lead

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "segmentos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(120), nullable=False, unique=True),
        sa.Column("palavras_chave", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "roteiros",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("segmento_id", sa.Integer(), sa.ForeignKey("segmentos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("porte", sa.String(10), nullable=False),
        sa.Column("foco", sa.String(200), nullable=False),
        sa.Column("etapa", sa.String(20), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.UniqueConstraint("segmento_id", "porte", "etapa", name="uq_roteiros_segmento_porte_etapa"),
    )
    op.create_index("ix_roteiros_segmento_id", "roteiros", ["segmento_id"])
    op.create_table(
        "mensagens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("numero_id", sa.Integer(), sa.ForeignKey("whatsapp_numbers.id")),
        sa.Column("direcao", sa.String(10), nullable=False),
        sa.Column("autor", sa.String(10), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("evolution_message_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for coluna in ("lead_id", "evolution_message_id", "created_at"):
        op.create_index(f"ix_mensagens_{coluna}", "mensagens", [coluna])

    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("segmento", sa.String(120)))
        batch.add_column(sa.Column("porte", sa.String(10)))
        batch.add_column(sa.Column("etapa", sa.String(20)))
        batch.add_column(sa.Column("ia_ativa", sa.Boolean(), server_default=sa.true(), nullable=False))
        batch.add_column(sa.Column("handoff_motivo", sa.String(300)))
        batch.add_column(sa.Column("responder_em", sa.DateTime(timezone=True)))
    with op.batch_alter_table("whatsapp_numbers") as batch:
        batch.add_column(sa.Column("webhook_versao", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("whatsapp_numbers") as batch:
        batch.drop_column("webhook_versao")
    with op.batch_alter_table("leads") as batch:
        for coluna in ("responder_em", "handoff_motivo", "ia_ativa", "etapa", "porte", "segmento"):
            batch.drop_column(coluna)
    for coluna in ("lead_id", "evolution_message_id", "created_at"):
        op.drop_index(f"ix_mensagens_{coluna}", table_name="mensagens")
    op.drop_table("mensagens")
    op.drop_index("ix_roteiros_segmento_id", table_name="roteiros")
    op.drop_table("roteiros")
    op.drop_table("segmentos")
