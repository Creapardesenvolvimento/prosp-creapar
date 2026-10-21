"""tipo da campanha: normal ou super prospecção

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campanhas") as batch:
        batch.add_column(sa.Column("modo", sa.String(10), server_default="normal", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("campanhas") as batch:
        batch.drop_column("modo")
