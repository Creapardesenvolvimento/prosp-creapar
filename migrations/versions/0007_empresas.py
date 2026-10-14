"""empresas: todos os resultados das buscas do Apify

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "empresas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("busca_id", sa.Integer(), sa.ForeignKey("buscas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("place_id", sa.String(200)),
        sa.Column("nome", sa.String(300), nullable=False),
        sa.Column("telefone_original", sa.String(50)),
        sa.Column("telefone", sa.String(20)),
        sa.Column("celular", sa.Boolean(), nullable=False),
        sa.Column("site", sa.String(500)),
        sa.Column("categoria", sa.String(200)),
        sa.Column("endereco", sa.String(500)),
        sa.Column("bairro", sa.String(200)),
        sa.Column("cidade", sa.String(200)),
        sa.Column("estado", sa.String(100)),
        sa.Column("nota", sa.Float()),
        sa.Column("avaliacoes", sa.Integer()),
        sa.Column("url_maps", sa.String(1000)),
        sa.Column("descarte", sa.String(40)),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id")),
        sa.Column("dados", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("busca_id", "place_id", name="uq_empresas_busca_place"),
    )
    for coluna in ("busca_id", "place_id", "telefone", "lead_id"):
        op.create_index(f"ix_empresas_{coluna}", "empresas", [coluna])


def downgrade() -> None:
    for coluna in ("busca_id", "place_id", "telefone", "lead_id"):
        op.drop_index(f"ix_empresas_{coluna}", table_name="empresas")
    op.drop_table("empresas")
