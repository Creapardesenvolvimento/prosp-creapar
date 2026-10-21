"""abastecimento automático de buscas e proxy por número

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plano_buscas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("termo", sa.String(200), nullable=False),
        sa.Column("cidade", sa.String(200), nullable=False),
        sa.Column("limite", sa.Integer(), nullable=False),
        sa.Column("campanha_id", sa.Integer(), sa.ForeignKey("campanhas.id", ondelete="SET NULL")),
        sa.Column("ativo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("busca_id", sa.Integer(), sa.ForeignKey("buscas.id", ondelete="SET NULL")),
        sa.Column("buscado_em", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    with op.batch_alter_table("whatsapp_numbers") as batch:
        batch.add_column(sa.Column("proxy_protocolo", sa.String(10)))
        batch.add_column(sa.Column("proxy_host", sa.String(255)))
        batch.add_column(sa.Column("proxy_porta", sa.String(10)))
        batch.add_column(sa.Column("proxy_usuario", sa.String(255)))
        batch.add_column(sa.Column("proxy_senha", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("whatsapp_numbers") as batch:
        for coluna in ("proxy_senha", "proxy_usuario", "proxy_porta", "proxy_host", "proxy_protocolo"):
            batch.drop_column(coluna)
    op.drop_table("plano_buscas")
