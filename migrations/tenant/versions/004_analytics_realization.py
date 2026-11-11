"""Select the latest suitable import and expose four realization sheets.

Duplicate rows are not a reason to exclude an import. A separate view hides
only the second row of an adjacent identical pair in October 2026 realization
versions 2, 3, and 4.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_analytics_realization"
down_revision: str | None = "003_file_sha"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REALIZATION_VIEWS = (
    "realization_delivered",
    "realization_transferred",
    "realization_returned",
    "realization_unredeemed",
)
_READABLE = (
    "analytics.import_exclusions",
    "analytics.selected_import",
    "analytics.realization_suppressed_duplicate",
    *(f"analytics.{name}" for name in _REALIZATION_VIEWS),
)

def upgrade() -> None:
    op.create_table(
        "import_exclusions",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "import_id",
            sa.Uuid(),
            sa.ForeignKey("raw.report_imports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column(
            "excluded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_import_exclusions_reason"),
        sa.CheckConstraint("btrim(basis) <> ''", name="ck_import_exclusions_basis"),
        schema="analytics",
    )
    op.create_index(
        "uq_import_exclusions_active",
        "import_exclusions",
        ["import_id"],
        unique=True,
        schema="analytics",
        postgresql_where=sa.text("lifted_at IS NULL"),
    )
    op.execute(_SELECTED_IMPORT)
    op.execute(_SUPPRESSED_DUPLICATE)
    for name, sheet, metrics in _SHEETS:
        op.execute(_realization_view(name, sheet, metrics))
    _grant_datalens()


def downgrade() -> None:
    for name in reversed(_REALIZATION_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS analytics.{name}")
    op.execute("DROP VIEW IF EXISTS analytics.realization_suppressed_duplicate")
    op.execute("DROP VIEW IF EXISTS analytics.selected_import")
    op.drop_index(
        "uq_import_exclusions_active",
        table_name="import_exclusions",
        schema="analytics",
    )
    op.drop_table("import_exclusions", schema="analytics")


def _grant_datalens() -> None:
    connection = op.get_bind()
    exists = connection.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'profitsamurai_datalens'")
    ).scalar()
    if exists != 1:
        return
    for name in _READABLE:
        connection.execute(sa.text(f"REVOKE ALL ON {name} FROM profitsamurai_datalens"))
        connection.execute(sa.text(f"GRANT SELECT ON {name} TO profitsamurai_datalens"))


def _json_number(key: str, alias: str) -> str:
    return (
        "CASE WHEN jsonb_typeof(r.payload -> '"
        + key
        + "') = 'number' THEN (r.payload ->> '"
        + key
        + "')::numeric ELSE NULL END AS "
        + alias
    )


def _json_text(key: str, alias: str) -> str:
    return (
        "CASE WHEN jsonb_typeof(r.payload -> '"
        + key
        + "') = 'string' THEN r.payload ->> '"
        + key
        + "' ELSE NULL END AS "
        + alias
    )


def _json_bigint(key: str, alias: str) -> str:
    return (
        "CASE WHEN jsonb_typeof(r.payload -> '"
        + key
        + "') = 'number' THEN (r.payload ->> '"
        + key
        + "')::bigint ELSE NULL END AS "
        + alias
    )


def _realization_view(name: str, sheet: str, metrics: tuple[str, ...]) -> str:
    columns = ",\n".join(
        (
            "s.import_id",
            "r.sheet",
            "r.row_number",
            "s.shop_id",
            "s.business_id",
            "s.period_from",
            "s.period_to",
            "s.campaign_id",
            "s.version",
            "c.placement_type",
            _json_bigint("orderId", "order_id"),
            _json_text("yourOrderId", "your_order_id"),
            _json_text("yourSku", "your_sku"),
            _json_text("orderCreationDate", "order_creation_date"),
            _json_text("transferredToDeliveryDate", "transferred_to_delivery_date"),
            _json_text("deliveryDate", "delivery_date"),
            *metrics,
            "r.payload",
        )
    )
    return f"""
CREATE VIEW analytics.{name} AS
SELECT
{columns}
FROM raw.report_rows AS r
JOIN analytics.selected_import AS s
  ON s.import_id = r.import_id
LEFT JOIN ref.campaigns AS c
  ON c.campaign_id = s.campaign_id
 AND c.business_id = s.business_id
WHERE s.report_type = 'goods_realization'
  AND r.sheet = '{sheet}'
  AND NOT EXISTS (
    SELECT 1
    FROM analytics.realization_suppressed_duplicate AS suppressed
    WHERE suppressed.import_id = r.import_id
      AND suppressed.sheet = r.sheet
      AND suppressed.row_number = r.row_number
  )
"""


_SUPPRESSED_DUPLICATE = """
CREATE VIEW analytics.realization_suppressed_duplicate AS
SELECT
  later.import_id,
  later.sheet,
  later.row_number,
  earlier.row_number AS kept_row_number,
  later.payload,
  'Временная техническая защита: вторая строка соседней пары с тем же payload '
  || 'в октябрьской реализации версии 2, 3 или 4. Это не правило Яндекс Маркета.'
    AS reason
FROM raw.report_rows AS later
JOIN raw.report_rows AS earlier
  ON earlier.import_id = later.import_id
 AND earlier.sheet = later.sheet
 AND earlier.row_number = later.row_number - 1
 AND earlier.payload = later.payload
JOIN raw.report_imports AS imports
  ON imports.id = later.import_id
WHERE imports.report_type = 'goods_realization'
  AND imports.period_from = DATE '2026-10-01'
  AND imports.period_to = DATE '2026-10-31'
  AND imports.version IN (2, 3, 4)
  AND later.sheet IN (
    'delivered',
    'transferred_to_delivery',
    'returned',
    'unredeemed'
  )
"""

_SELECTED_IMPORT = """
CREATE VIEW analytics.selected_import AS
SELECT
  chosen.id AS import_id,
  chosen.shop_id,
  chosen.business_id,
  chosen.report_type,
  chosen.period_from,
  chosen.period_to,
  chosen.campaign_id,
  chosen.version,
  chosen.status,
  chosen.started_at,
  chosen.finished_at
FROM (
  SELECT
    imports.*,
    row_number() OVER (
      PARTITION BY
        imports.shop_id,
        imports.business_id,
        imports.report_type,
        imports.period_from,
        imports.period_to,
        imports.campaign_id
      ORDER BY
        imports.finished_at DESC NULLS LAST,
        imports.version DESC,
        imports.id DESC
    ) AS selection_rank
  FROM raw.report_imports AS imports
  WHERE imports.status = 'done'
    AND NOT EXISTS (
      SELECT 1
      FROM analytics.import_exclusions AS exclusions
      WHERE exclusions.import_id = imports.id
        AND exclusions.lifted_at IS NULL
    )
) AS chosen
WHERE chosen.selection_rank = 1
"""

_SHEETS = (
    (
        "realization_delivered",
        "delivered",
        (
            _json_number("deliveredCount", "delivered_count"),
            _json_number("deliveredPriceSumWithVatAndDiscounts", "delivered_price_sum"),
        ),
    ),
    (
        "realization_transferred",
        "transferred_to_delivery",
        (
            _json_number("transferredToDeliveryCount", "transferred_count"),
            _json_number(
                "transferredToDeliveryPriceSumWithVatAndDiscounts",
                "transferred_price_sum",
            ),
        ),
    ),
    (
        "realization_returned",
        "returned",
        (
            _json_number("returnedCount", "returned_count"),
            _json_number("returnPriceSumWithVatAndDiscounts", "return_price_sum"),
            _json_text("returnWarehouseOrScAcceptDate", "return_accept_date"),
        ),
    ),
    (
        "realization_unredeemed",
        "unredeemed",
        (
            _json_number("unredeemedCount", "unredeemed_count"),
            _json_number("unredeemedPriceSumWithVatAndDiscounts", "unredeemed_price_sum"),
            _json_text("unredeemedWarehouseOrScAcceptDate", "unredeemed_accept_date"),
        ),
    ),
)
