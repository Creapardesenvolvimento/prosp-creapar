"""evolution_servers e whatsapp_numbers

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evolution_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(120), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("ativo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "whatsapp_numbers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), sa.ForeignKey("evolution_servers.id"), nullable=False),
        sa.Column("nome_exibicao", sa.String(120), nullable=False),
        sa.Column("instance_name", sa.String(120), nullable=False, unique=True),
        sa.Column("telefone", sa.String(20)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("data_ativacao", sa.DateTime(timezone=True)),
        sa.Column("limite_diario_atual", sa.Integer(), nullable=False),
        sa.Column("envios_hoje", sa.Integer(), nullable=False),
        sa.Column("chatwoot_inbox_id", sa.Integer()),
        sa.Column("substitui_numero_id", sa.Integer(), sa.ForeignKey("whatsapp_numbers.id")),
        sa.Column("motivo_desconexao", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_whatsapp_numbers_status", "whatsapp_numbers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_whatsapp_numbers_status", table_name="whatsapp_numbers")
    op.drop_table("whatsapp_numbers")
    op.drop_table("evolution_servers")
