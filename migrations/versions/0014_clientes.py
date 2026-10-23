"""vários clientes: tudo o que existe passa a ser do cliente Brafort10

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

TABELAS = [
    "whatsapp_numbers", "campanhas", "envios", "buscas", "plano_buscas", "leads", "empresas", "blacklist",
    "segmentos", "roteiros", "vendedores", "reunioes", "mensagens",
]
# Únicos que passam a valer por cliente: (tabela, coluna, nome novo)
UNICOS = [
    ("leads", "telefone", "uq_leads_cliente_telefone"),
    ("blacklist", "telefone", "uq_blacklist_cliente_telefone"),
    ("segmentos", "nome", "uq_segmentos_cliente_nome"),
]
BRAFORT = "(SELECT id FROM clientes WHERE nome = 'Brafort10')"


def _nome_unico_antigo(tabela: str, coluna: str) -> str:
    # No Postgres a restrição sem nome ganhou o nome padrão; no SQLite o batch usa a convenção abaixo.
    return f"{tabela}_{coluna}_key" if op.get_bind().dialect.name == "postgresql" else f"uq_{tabela}_{coluna}"


CONVENCAO = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    clientes = op.create_table(
        "clientes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(120), nullable=False, unique=True),
        sa.Column("ativo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("arquivado_em", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(clientes, [{"nome": "Brafort10", "ativo": True}])

    for tabela in TABELAS:
        with op.batch_alter_table(tabela) as batch:
            batch.add_column(sa.Column("cliente_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {tabela} SET cliente_id = {BRAFORT}")
        with op.batch_alter_table(tabela) as batch:
            batch.alter_column("cliente_id", existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(f"fk_{tabela}_cliente_id", "clientes", ["cliente_id"], ["id"], ondelete="CASCADE")
            batch.create_index(f"ix_{tabela}_cliente_id", ["cliente_id"])

    for tabela, coluna, novo in UNICOS:
        with op.batch_alter_table(tabela, naming_convention=CONVENCAO) as batch:
            batch.drop_constraint(_nome_unico_antigo(tabela, coluna), type_="unique")
            batch.create_unique_constraint(novo, ["cliente_id", coluna])

    # Configurações: a chave primária era a própria chave; agora é id, com chave única por cliente.
    op.create_table(
        "configuracoes_nova",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliente_id", sa.Integer(), sa.ForeignKey("clientes.id", ondelete="CASCADE", name="fk_configuracoes_cliente_id"), nullable=False),
        sa.Column("chave", sa.String(60), nullable=False),
        sa.Column("valor", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("cliente_id", "chave", name="uq_configuracoes_cliente_chave"),
    )
    op.execute(
        f"INSERT INTO configuracoes_nova (cliente_id, chave, valor, updated_at) SELECT {BRAFORT}, chave, valor, updated_at FROM configuracoes"
    )
    op.drop_table("configuracoes")
    op.rename_table("configuracoes_nova", "configuracoes")
    op.create_index("ix_configuracoes_cliente_id", "configuracoes", ["cliente_id"])


def downgrade() -> None:
    # Volta a um cliente só: fica o que é da Brafort10 (o primeiro cliente).
    primeiro = "(SELECT MIN(id) FROM clientes)"
    op.drop_index("ix_configuracoes_cliente_id", table_name="configuracoes")
    op.create_table(
        "configuracoes_antiga",
        sa.Column("chave", sa.String(60), primary_key=True),
        sa.Column("valor", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        f"INSERT INTO configuracoes_antiga (chave, valor, updated_at) SELECT chave, valor, updated_at FROM configuracoes WHERE cliente_id = {primeiro}"
    )
    op.drop_table("configuracoes")
    op.rename_table("configuracoes_antiga", "configuracoes")

    for tabela, coluna, novo in UNICOS:
        with op.batch_alter_table(tabela) as batch:
            batch.drop_constraint(novo, type_="unique")
    for tabela in reversed(TABELAS):
        op.execute(f"DELETE FROM {tabela} WHERE cliente_id <> {primeiro}")
        with op.batch_alter_table(tabela) as batch:
            batch.drop_index(f"ix_{tabela}_cliente_id")
            batch.drop_constraint(f"fk_{tabela}_cliente_id", type_="foreignkey")
            batch.drop_column("cliente_id")
    for tabela, coluna, _ in UNICOS:
        with op.batch_alter_table(tabela, naming_convention=CONVENCAO) as batch:
            batch.create_unique_constraint(_nome_unico_antigo(tabela, coluna), [coluna])
    op.drop_table("clientes")
