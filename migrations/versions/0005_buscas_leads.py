"""buscas, leads e blacklist

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buscas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("termo", sa.String(200), nullable=False),
        sa.Column("cidade", sa.String(200), nullable=False),
        sa.Column("limite", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("apify_run_id", sa.String(64), unique=True),
        sa.Column("apify_dataset_id", sa.String(64)),
        sa.Column("total_encontrados", sa.Integer(), nullable=False),
        sa.Column("total_novos", sa.Integer(), nullable=False),
        sa.Column("descartes", sa.JSON(), nullable=False),
        sa.Column("erro", sa.Text()),
        sa.Column("criado_por_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_buscas_status", "buscas", ["status"])
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("busca_id", sa.Integer(), sa.ForeignKey("buscas.id")),
        sa.Column("nome", sa.String(300), nullable=False),
        sa.Column("telefone", sa.String(20), nullable=False, unique=True),
        sa.Column("whatsapp_jid", sa.String(64)),
        sa.Column("site", sa.String(500)),
        sa.Column("categoria", sa.String(200)),
        sa.Column("endereco", sa.String(500)),
        sa.Column("cidade", sa.String(200)),
        sa.Column("nota", sa.Float()),
        sa.Column("avaliacoes", sa.Integer()),
        sa.Column("email", sa.String(255)),
        sa.Column("place_id", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_leads_busca_id", "leads", ["busca_id"])
    op.create_index("ix_leads_status", "leads", ["status"])
    op.create_table(
        "blacklist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telefone", sa.String(20), nullable=False, unique=True),
        sa.Column("motivo", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("blacklist")
    op.drop_index("ix_leads_status", table_name="leads")
    op.drop_index("ix_leads_busca_id", table_name="leads")
    op.drop_table("leads")
    op.drop_index("ix_buscas_status", table_name="buscas")
    op.drop_table("buscas")
