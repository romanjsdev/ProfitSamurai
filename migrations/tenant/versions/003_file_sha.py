"""SHA-256 of each stored report file. File bytes and report rows stay as they are."""

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_file_sha"
down_revision: str | None = "002_finance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_files",
        sa.Column("content_sha256", sa.Text(), nullable=True),
        schema="raw",
    )
    connection = op.get_bind()
    stored = connection.execute(sa.text("SELECT id, content FROM raw.report_files"))
    for file_id, content in stored:
        payload = content if isinstance(content, bytes) else bytes(content)
        connection.execute(
            sa.text("UPDATE raw.report_files SET content_sha256 = :digest WHERE id = :id"),
            {"digest": hashlib.sha256(payload).hexdigest(), "id": file_id},
        )
    op.alter_column("report_files", "content_sha256", nullable=False, schema="raw")
    op.create_check_constraint(
        "ck_report_files_content_sha256",
        "report_files",
        "content_sha256 ~ '^[0-9a-f]{64}$'",
        schema="raw",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_report_files_content_sha256",
        "report_files",
        schema="raw",
        type_="check",
    )
    op.drop_column("report_files", "content_sha256", schema="raw")
