"""Drop the prototype cabinet tables after the live shop is stored in the new model."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.util.concurrency import await_only

from profitsamurai.db.prototype_guard import (
    PROTOTYPE_BUSINESS_ID,
    PROTOTYPE_PLACEMENTS,
    prototype_data_is_present,
    require_prototype_replacement,
)

revision: str = "003_drop_prototype"
down_revision: str | None = "002_client_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if _prototype_rows_present(bind):
        _require_live_replacement(bind)
    op.execute(text("DROP TABLE IF EXISTS public.campaigns"))
    op.execute(text("DROP TABLE IF EXISTS public.cabinets"))


def downgrade() -> None:
    op.create_table(
        "cabinets",
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("api_key_ciphertext", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("business_id"),
    )
    op.create_table(
        "campaigns",
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["cabinets.business_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("campaign_id"),
    )
    op.create_index("ix_campaigns_business_id", "campaigns", ["business_id"])


def _prototype_rows_present(bind: Connection) -> bool:
    cabinet_ids = _optional_ids(
        bind,
        "public.cabinets",
        "SELECT business_id FROM public.cabinets",
    )
    campaign_ids = _optional_ids(
        bind,
        "public.campaigns",
        "SELECT campaign_id FROM public.campaigns",
    )
    return prototype_data_is_present(cabinet_ids, campaign_ids)


def _optional_ids(bind: Connection, relation: str, query: str) -> set[int]:
    if bind.execute(text("SELECT to_regclass(:relation)"), {"relation": relation}).scalar() is None:
        return set()
    return {int(value) for value in bind.execute(text(query)).scalars()}


def _require_live_replacement(bind: Connection) -> None:
    import os

    rows = bind.execute(
        text(
            "SELECT s.connection_status, s.api_key_ciphertext, d.database_name "
            "FROM public.shops s "
            "JOIN public.client_databases d ON d.client_id = s.client_id "
            "WHERE s.business_id = :business_id"
        ),
        {"business_id": PROTOTYPE_BUSINESS_ID},
    ).all()
    shops = [
        (str(row[0]), bytes(row[1]), str(row[2]))
        for row in rows
    ]
    service_url = bind.engine.url.render_as_string(hide_password=False)
    campaigns_by_database = {
        database_name: _placements(service_url, database_name)
        for database_name in {shop[2] for shop in shops}
    }
    require_prototype_replacement(shops, campaigns_by_database, os.environ.get("FERNET_KEY", ""))


def _placements(service_url: str, database_name: str) -> dict[int, str | None]:
    async def read() -> dict[int, str | None]:
        url = make_url(service_url).set(database=database_name)
        engine = create_async_engine(url.render_as_string(hide_password=False))
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT campaign_id, placement_type FROM ref.campaigns "
                        "WHERE campaign_id = ANY(:campaign_ids)"
                    ),
                    {"campaign_ids": list(PROTOTYPE_PLACEMENTS)},
                )
                return {
                    int(row[0]): row[1] if isinstance(row[1], str) else None
                    for row in result
                }
        finally:
            await engine.dispose()

    return await_only(read())
