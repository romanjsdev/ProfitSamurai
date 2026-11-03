"""Initial service registry for an empty database."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_registry"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "client_databases",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("database_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed')",
            name="ck_client_databases_status",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("client_id"),
        sa.UniqueConstraint("database_name"),
    )
    op.create_table(
        "shops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("marketplace", sa.Text(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("api_key_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("connection_status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "connection_status IN ('connected', 'error')",
            name="ck_shops_connection_status",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_id",
            "marketplace",
            "business_id",
            name="uq_shops_client_marketplace_business",
        ),
    )
    op.create_index("ix_shops_client_id", "shops", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_shops_client_id", table_name="shops")
    op.drop_table("shops")
    op.drop_table("client_databases")
    op.drop_table("clients")
