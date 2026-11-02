"""Empty client database: schemas and campaign placement."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_tenant"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS raw")
    op.execute("CREATE SCHEMA IF NOT EXISTS ref")
    op.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    op.create_table(
        "campaigns",
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("placement_type", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("campaign_id"),
        schema="ref",
    )
    op.create_index("ix_ref_campaigns_business_id", "campaigns", ["business_id"], schema="ref")


def downgrade() -> None:
    op.drop_index("ix_ref_campaigns_business_id", table_name="campaigns", schema="ref")
    op.drop_table("campaigns", schema="ref")
    op.execute("DROP SCHEMA IF EXISTS analytics")
    op.execute("DROP SCHEMA IF EXISTS ref")
    op.execute("DROP SCHEMA IF EXISTS raw")
