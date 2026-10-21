"""importação de listas de contatos

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("buscas") as batch:
        batch.add_column(sa.Column("origem", sa.String(20), server_default="apify", nullable=False))
        batch.add_column(sa.Column("campanha_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_buscas_campanha_id", "campanhas", ["campanha_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("buscas") as batch:
        batch.drop_constraint("fk_buscas_campanha_id", type_="foreignkey")
        batch.drop_column("campanha_id")
        batch.drop_column("origem")
