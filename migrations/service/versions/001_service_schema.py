"""Service database: cabinets and their campaign ids."""

from collections.abc import Sequence

from alembic import op

from profitsamurai.db.service.models import ServiceBase

revision: str = "001_service"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ServiceBase.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    ServiceBase.metadata.drop_all(bind=op.get_bind())
