"""follow-up automático

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campanha_followups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campanha_id", sa.Integer(), sa.ForeignKey("campanhas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("passo", sa.Integer(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_campanha_followups_campanha_id", "campanha_followups", ["campanha_id"])
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("followups_enviados", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("ultimo_followup_em", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("ultimo_followup_em")
        batch.drop_column("followups_enviados")
    op.drop_index("ix_campanha_followups_campanha_id", table_name="campanha_followups")
    op.drop_table("campanha_followups")
