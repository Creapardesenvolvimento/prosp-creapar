"""agenda interna: vendedores, horários, bloqueios e reuniões

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendedores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(120), nullable=False),
        sa.Column("telefone", sa.String(20)),
        sa.Column("email", sa.String(255)),
        sa.Column("link_reuniao", sa.String(500)),
        sa.Column("ativo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "vendedor_horarios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendedor_id", sa.Integer(), sa.ForeignKey("vendedores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dia_semana", sa.Integer(), nullable=False),
        sa.Column("inicio", sa.Time(), nullable=False),
        sa.Column("fim", sa.Time(), nullable=False),
    )
    op.create_index("ix_vendedor_horarios_vendedor_id", "vendedor_horarios", ["vendedor_id"])
    op.create_table(
        "vendedor_bloqueios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendedor_id", sa.Integer(), sa.ForeignKey("vendedores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inicio", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fim", sa.DateTime(timezone=True), nullable=False),
        sa.Column("motivo", sa.String(200), nullable=False),
    )
    op.create_index("ix_vendedor_bloqueios_vendedor_id", "vendedor_bloqueios", ["vendedor_id"])
    op.create_table(
        "reunioes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendedor_id", sa.Integer(), sa.ForeignKey("vendedores.id"), nullable=False),
        sa.Column("lead_id", sa.Integer()),
        sa.Column("lead_nome", sa.String(200), nullable=False),
        sa.Column("lead_telefone", sa.String(20)),
        sa.Column("inicio", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fim", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("link", sa.String(500)),
        sa.Column("observacoes", sa.Text(), nullable=False),
        sa.Column("vendedor_avisado_em", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_reunioes_vendedor_id", "reunioes", ["vendedor_id"])
    op.create_index("ix_reunioes_inicio", "reunioes", ["inicio"])
    op.create_index(
        "uq_reunioes_vendedor_inicio_agendada",
        "reunioes",
        ["vendedor_id", "inicio"],
        unique=True,
        postgresql_where=sa.text("status = 'agendada'"),
        sqlite_where=sa.text("status = 'agendada'"),
    )


def downgrade() -> None:
    op.drop_index("uq_reunioes_vendedor_inicio_agendada", table_name="reunioes")
    op.drop_index("ix_reunioes_inicio", table_name="reunioes")
    op.drop_index("ix_reunioes_vendedor_id", table_name="reunioes")
    op.drop_table("reunioes")
    op.drop_index("ix_vendedor_bloqueios_vendedor_id", table_name="vendedor_bloqueios")
    op.drop_table("vendedor_bloqueios")
    op.drop_index("ix_vendedor_horarios_vendedor_id", table_name="vendedor_horarios")
    op.drop_table("vendedor_horarios")
    op.drop_table("vendedores")
