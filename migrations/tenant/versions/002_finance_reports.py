"""Raw tables for financial report files and rows."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_finance"
down_revision: str | None = "001_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("yandex_report_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "report_type IN ('goods_realization', 'marketplace_services', 'united_netting')",
            name="ck_report_imports_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'no_data', 'too_large', 'failed')",
            name="ck_report_imports_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shop_id",
            "period_from",
            "period_to",
            "report_type",
            "campaign_id",
            "version",
            name="uq_report_imports_version",
            postgresql_nulls_not_distinct=True,
        ),
        schema="raw",
    )
    op.create_index(
        "ix_raw_report_imports_period",
        "report_imports",
        ["shop_id", "period_from", "period_to"],
        schema="raw",
    )
    op.create_table(
        "report_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["raw.report_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id"),
        schema="raw",
    )
    op.create_table(
        "report_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("sheet", sa.Text(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["raw.report_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="raw",
    )
    op.create_index(
        "ix_raw_report_rows_import_id",
        "report_rows",
        ["import_id"],
        schema="raw",
    )


def downgrade() -> None:
    op.drop_index("ix_raw_report_rows_import_id", table_name="report_rows", schema="raw")
    op.drop_table("report_rows", schema="raw")
    op.drop_table("report_files", schema="raw")
    op.drop_index("ix_raw_report_imports_period", table_name="report_imports", schema="raw")
    op.drop_table("report_imports", schema="raw")
