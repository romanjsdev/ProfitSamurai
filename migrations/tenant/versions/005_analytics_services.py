"""Expose marketplace service charges, netting movements, and bank orders.

Rows come from the import chosen by analytics.selected_import. Service and
netting files in the studied months have no adjacent duplicate pairs, so this
revision does not add a deduplication rule for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_analytics_services"
down_revision: str | None = "004_analytics_realization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEWS = (
    "service_sku_charge",
    "service_order_charge",
    "netting_movement",
    "bank_order",
    "bank_order_conflict",
)
_PRICE_SHEETS = (
    "boost",
    "delivery",
    "crossregional_delivery",
    "payment_accepting",
    "payment_transfer",
    "order_processing",
    "order_processing_on_warehouse",
    "storage_of_returns",
)


def upgrade() -> None:
    op.execute(_service_view("service_sku_charge", sku_present=True))
    op.execute(_service_view("service_order_charge", sku_present=False))
    op.execute(_NETTING)
    op.execute(_BANK_ORDER)
    op.execute(_BANK_CONFLICT)
    _grant_datalens()


def downgrade() -> None:
    for name in reversed(_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS analytics.{name}")


def _grant_datalens() -> None:
    connection = op.get_bind()
    exists = connection.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'profitsamurai_datalens'")
    ).scalar()
    if exists != 1:
        return
    for name in _VIEWS:
        qualified = f"analytics.{name}"
        connection.execute(sa.text(f"REVOKE ALL ON {qualified} FROM profitsamurai_datalens"))
        connection.execute(sa.text(f"GRANT SELECT ON {qualified} TO profitsamurai_datalens"))


def _number(key: str) -> str:
    return (
        "CASE WHEN jsonb_typeof(r.payload -> '"
        + key
        + "') = 'number' THEN (r.payload ->> '"
        + key
        + "')::numeric END"
    )


def _text(key: str) -> str:
    return (
        "CASE WHEN jsonb_typeof(r.payload -> '"
        + key
        + "') = 'string' AND btrim(r.payload ->> '"
        + key
        + "') <> '' THEN r.payload ->> '"
        + key
        + "' END"
    )


def _bigint(key: str) -> str:
    return (
        "CASE WHEN jsonb_typeof(r.payload -> '"
        + key
        + "') = 'number' THEN (r.payload ->> '"
        + key
        + "')::bigint END"
    )


def _amount_expr() -> str:
    lines = [
        "CASE r.sheet",
        "  WHEN 'placement' THEN " + _number("totalAmount"),
        "  WHEN 'paid_storage_after_01-09-26' THEN " + _number("paidStorage"),
    ]
    for sheet in _PRICE_SHEETS:
        lines.append(f"  WHEN '{sheet}' THEN " + _number("servicePrice"))
    lines.append("  ELSE NULL")
    lines.append("END")
    return "\n".join(lines)


def _amount_field_expr() -> str:
    lines = [
        "CASE r.sheet",
        "  WHEN 'placement' THEN 'totalAmount'",
        "  WHEN 'paid_storage_after_01-09-26' THEN 'paidStorage'",
    ]
    for sheet in _PRICE_SHEETS:
        lines.append(f"  WHEN '{sheet}' THEN 'servicePrice'")
    lines.append("  ELSE NULL")
    lines.append("END")
    return "\n".join(lines)


def _service_view(name: str, *, sku_present: bool) -> str:
    sku_test = (
        "coalesce("
        "jsonb_typeof(r.payload -> 'shopSku') = 'string' "
        "AND btrim(r.payload ->> 'shopSku') <> '', false)"
    )
    where_sku = sku_test if sku_present else "NOT " + sku_test
    columns = ",\n".join(
        (
            "s.import_id",
            "files.id AS file_id",
            "r.sheet",
            "r.row_number",
            "s.shop_id",
            "s.business_id",
            "s.period_from",
            "s.period_to",
            "s.campaign_id",
            "s.version",
            _bigint("partnerId") + " AS partner_id",
            _bigint("orderId") + " AS order_id",
            _text("shopSku") + " AS shop_sku",
            _text("serviceName") + " AS service_name",
            _text("serviceDateTime") + " AS service_datetime",
            _text("serviceDate") + " AS service_date",
            _text("actDate") + " AS act_date",
            _amount_expr() + " AS amount",
            _amount_field_expr() + " AS amount_field",
            _text("currency") + " AS currency",
            _text("placementModel") + " AS placement_model",
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
LEFT JOIN raw.report_files AS files
  ON files.import_id = s.import_id
WHERE s.report_type = 'marketplace_services'
  AND {where_sku}
"""


_NETTING = """
CREATE VIEW analytics.netting_movement AS
SELECT
  s.import_id,
  files.id AS file_id,
  r.sheet,
  r.row_number,
  s.shop_id,
  s.business_id,
  s.period_from,
  s.period_to,
  s.campaign_id,
  s.version,
  CASE WHEN jsonb_typeof(r.payload -> 'partnerId') = 'number'
    THEN (r.payload ->> 'partnerId')::bigint END AS partner_id,
  CASE WHEN jsonb_typeof(r.payload -> 'transactionDate') = 'string'
    THEN r.payload ->> 'transactionDate' END AS transaction_date,
  CASE WHEN jsonb_typeof(r.payload -> 'transactionSource') = 'string'
    THEN r.payload ->> 'transactionSource' END AS transaction_source,
  CASE WHEN jsonb_typeof(r.payload -> 'transactionType') = 'string'
    THEN r.payload ->> 'transactionType' END AS transaction_type,
  CASE WHEN jsonb_typeof(r.payload -> 'offerOrServiceName') = 'string'
    AND btrim(r.payload ->> 'offerOrServiceName') <> ''
    THEN r.payload ->> 'offerOrServiceName' END AS offer_or_service_name,
  CASE WHEN jsonb_typeof(r.payload -> 'transactionSum') = 'number'
    THEN (r.payload ->> 'transactionSum')::numeric END AS transaction_sum,
  CASE WHEN jsonb_typeof(r.payload -> 'paymentStatus') = 'string'
    THEN r.payload ->> 'paymentStatus' END AS payment_status,
  CASE WHEN jsonb_typeof(r.payload -> 'bonusAccountYearMonth') = 'string'
    AND btrim(r.payload ->> 'bonusAccountYearMonth') <> ''
    THEN r.payload ->> 'bonusAccountYearMonth' END AS bonus_account_year_month,
  CASE WHEN jsonb_typeof(r.payload -> 'bankOrderId') = 'number'
    THEN (r.payload ->> 'bankOrderId')::bigint END AS bank_order_id,
  CASE WHEN jsonb_typeof(r.payload -> 'bankOrderDate') = 'string'
    THEN r.payload ->> 'bankOrderDate' END AS bank_order_date,
  CASE WHEN jsonb_typeof(r.payload -> 'bankSum') = 'number'
    THEN (r.payload ->> 'bankSum')::numeric END AS bank_sum,
  CASE WHEN jsonb_typeof(r.payload -> 'orderId') = 'number'
    THEN (r.payload ->> 'orderId')::bigint END AS order_id,
  CASE WHEN jsonb_typeof(r.payload -> 'shopSku') = 'string'
    AND btrim(r.payload ->> 'shopSku') <> ''
    THEN r.payload ->> 'shopSku' END AS shop_sku,
  CASE WHEN jsonb_typeof(r.payload -> 'actId') = 'string'
    AND btrim(r.payload ->> 'actId') <> ''
    THEN r.payload ->> 'actId' END AS act_id,
  CASE WHEN jsonb_typeof(r.payload -> 'transactionId') = 'string'
    AND btrim(r.payload ->> 'transactionId') <> ''
    THEN r.payload ->> 'transactionId'
    WHEN jsonb_typeof(r.payload -> 'transactionId') = 'number'
    THEN r.payload ->> 'transactionId'
  END AS transaction_id,
  r.payload
FROM raw.report_rows AS r
JOIN analytics.selected_import AS s
  ON s.import_id = r.import_id
LEFT JOIN raw.report_files AS files
  ON files.import_id = s.import_id
WHERE s.report_type = 'united_netting'
  AND r.sheet = 'transaction_date'
"""

_BANK_GROUP = """
SELECT
  s.import_id,
  files.id AS file_id,
  s.shop_id,
  s.business_id,
  s.period_from,
  s.period_to,
  s.campaign_id,
  s.version,
  (r.payload ->> 'bankOrderId')::bigint AS bank_order_id,
  count(DISTINCT r.payload ->> 'bankSum') FILTER (
    WHERE jsonb_typeof(r.payload -> 'bankSum') = 'number'
  ) AS sum_count,
  min((r.payload ->> 'bankSum')::numeric) FILTER (
    WHERE jsonb_typeof(r.payload -> 'bankSum') = 'number'
  ) AS only_sum,
  count(DISTINCT r.payload ->> 'bankOrderDate') FILTER (
    WHERE jsonb_typeof(r.payload -> 'bankOrderDate') = 'string'
  ) AS date_count,
  min(r.payload ->> 'bankOrderDate') FILTER (
    WHERE jsonb_typeof(r.payload -> 'bankOrderDate') = 'string'
  ) AS only_date,
  count(DISTINCT r.payload ->> 'paymentStatus') FILTER (
    WHERE jsonb_typeof(r.payload -> 'paymentStatus') = 'string'
  ) AS status_count,
  min(r.payload ->> 'paymentStatus') FILTER (
    WHERE jsonb_typeof(r.payload -> 'paymentStatus') = 'string'
  ) AS only_status,
  count(*) AS movement_count
FROM raw.report_rows AS r
JOIN analytics.selected_import AS s
  ON s.import_id = r.import_id
LEFT JOIN raw.report_files AS files
  ON files.import_id = s.import_id
WHERE s.report_type = 'united_netting'
  AND r.sheet = 'transaction_date'
  AND jsonb_typeof(r.payload -> 'bankOrderId') = 'number'
GROUP BY
  s.import_id,
  files.id,
  s.shop_id,
  s.business_id,
  s.period_from,
  s.period_to,
  s.campaign_id,
  s.version,
  (r.payload ->> 'bankOrderId')::bigint
"""

_BANK_ORDER = f"""
CREATE VIEW analytics.bank_order AS
SELECT
  import_id,
  file_id,
  shop_id,
  business_id,
  period_from,
  period_to,
  campaign_id,
  version,
  bank_order_id,
  CASE WHEN date_count = 1 THEN only_date END AS bank_order_date,
  CASE WHEN sum_count = 1 THEN only_sum END AS bank_sum,
  CASE WHEN status_count = 1 THEN only_status END AS payment_status,
  movement_count
FROM (
{_BANK_GROUP}
) AS grouped
"""

_BANK_CONFLICT = f"""
CREATE VIEW analytics.bank_order_conflict AS
SELECT
  import_id,
  file_id,
  shop_id,
  business_id,
  period_from,
  period_to,
  campaign_id,
  version,
  bank_order_id,
  conflict_kind,
  distinct_count
FROM (
{_BANK_GROUP}
) AS grouped
CROSS JOIN LATERAL (
  VALUES
    ('bank_sum', sum_count),
    ('bank_order_date', date_count),
    ('payment_status', status_count)
) AS conflicts(conflict_kind, distinct_count)
WHERE distinct_count > 1
"""
