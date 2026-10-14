"""configurações, campanhas, variações, envios e campos de disparo

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configuracoes",
        sa.Column("chave", sa.String(60), primary_key=True),
        sa.Column("valor", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "campanhas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(200), nullable=False),
        sa.Column("nicho", sa.String(200), nullable=False),
        sa.Column("oferta", sa.Text(), nullable=False),
        sa.Column("prompt_agente", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_campanhas_status", "campanhas", ["status"])
    op.create_table(
        "campanha_mensagens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campanha_id", sa.Integer(), sa.ForeignKey("campanhas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("origem", sa.String(20), nullable=False),
        sa.Column("ativa", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_campanha_mensagens_campanha_id", "campanha_mensagens", ["campanha_id"])
    op.create_table(
        "campanha_numeros",
        sa.Column("campanha_id", sa.Integer(), sa.ForeignKey("campanhas.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("numero_id", sa.Integer(), sa.ForeignKey("whatsapp_numbers.id", ondelete="CASCADE"), primary_key=True),
    )

    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("campanha_id", sa.Integer()))
        batch.add_column(sa.Column("numero_id", sa.Integer()))
        batch.add_column(sa.Column("tentativas_envio", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("enviado_em", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("respondeu_em", sa.DateTime(timezone=True)))
        batch.create_foreign_key("fk_leads_campanha_id", "campanhas", ["campanha_id"], ["id"])
        batch.create_foreign_key("fk_leads_numero_id", "whatsapp_numbers", ["numero_id"], ["id"])
        batch.create_index("ix_leads_campanha_id", ["campanha_id"])
        batch.create_index("ix_leads_numero_id", ["numero_id"])

    with op.batch_alter_table("whatsapp_numbers") as batch:
        batch.add_column(sa.Column("proximo_envio_em", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("motivo_pausa", sa.String(200)))

    op.create_table(
        "envios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("campanha_id", sa.Integer(), sa.ForeignKey("campanhas.id")),
        sa.Column("numero_id", sa.Integer(), sa.ForeignKey("whatsapp_numbers.id"), nullable=False),
        sa.Column("mensagem_id", sa.Integer(), sa.ForeignKey("campanha_mensagens.id")),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("evolution_message_id", sa.String(100), unique=True),
        sa.Column("erro", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("enviado_em", sa.DateTime(timezone=True)),
    )
    for coluna in ("lead_id", "campanha_id", "numero_id", "enviado_em"):
        op.create_index(f"ix_envios_{coluna}", "envios", [coluna])


def downgrade() -> None:
    for coluna in ("lead_id", "campanha_id", "numero_id", "enviado_em"):
        op.drop_index(f"ix_envios_{coluna}", table_name="envios")
    op.drop_table("envios")
    with op.batch_alter_table("whatsapp_numbers") as batch:
        batch.drop_column("motivo_pausa")
        batch.drop_column("proximo_envio_em")
    with op.batch_alter_table("leads") as batch:
        batch.drop_index("ix_leads_numero_id")
        batch.drop_index("ix_leads_campanha_id")
        batch.drop_constraint("fk_leads_numero_id", type_="foreignkey")
        batch.drop_constraint("fk_leads_campanha_id", type_="foreignkey")
        batch.drop_column("respondeu_em")
        batch.drop_column("enviado_em")
        batch.drop_column("tentativas_envio")
        batch.drop_column("numero_id")
        batch.drop_column("campanha_id")
    op.drop_table("campanha_numeros")
    op.drop_index("ix_campanha_mensagens_campanha_id", table_name="campanha_mensagens")
    op.drop_table("campanha_mensagens")
    op.drop_index("ix_campanhas_status", table_name="campanhas")
    op.drop_table("campanhas")
    op.drop_table("configuracoes")
