"""reunioes.cancelamento_avisado_em

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reunioes", sa.Column("cancelamento_avisado_em", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("reunioes") as batch:
        batch.drop_column("cancelamento_avisado_em")
