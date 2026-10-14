"""aviso das reuniões no WhatsApp de notificações

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reunioes") as batch:
        batch.add_column(sa.Column("notificado_em", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("cancelamento_notificado_em", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("reunioes") as batch:
        batch.drop_column("cancelamento_notificado_em")
        batch.drop_column("notificado_em")
