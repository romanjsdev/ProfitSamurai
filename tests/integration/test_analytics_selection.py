"""Version selection and realization views against a disposable PostgreSQL.

Set RUN_INTEGRATION=1, INTEGRATION_DATABASE_URL, and INTEGRATION_ADMIN_URL.
The URLs are checked before any connection. The working cluster is rejected.
"""

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.analytics_exclusions import exclude_import, lift_import_exclusion

from .test_finance_reliability import _migrate, _tenant

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to use a disposable database",
)

_SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
_OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
_NOVEMBER = (date(2026, 11, 1), date(2026, 11, 30))
_FBY = 129958161
_FBS = 148633332


async def test_selection_follows_finish_time_status_and_exclusion() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = uuid4()
        other_shop_id = uuid4()

        only = await _import(tenant_url, shop_id=shop_id, version=1, finished_at=_at(1))
        assert await _selected_ids(tenant_url, shop_id) == {only}

        earlier = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            campaign_id=_FBS,
        )
        later_lower_version = await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(3),
            campaign_id=_FBS,
        )
        assert await _selected_ids(tenant_url, shop_id, campaign_id=_FBS) == {later_lower_version}
        assert earlier not in await _selected_ids(tenant_url, shop_id)

        await _import(
            tenant_url,
            shop_id=shop_id,
            version=3,
            status="failed",
            finished_at=_at(4),
            campaign_id=_FBS,
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=4,
            status="running",
            finished_at=None,
            campaign_id=_FBS,
        )
        assert await _selected_ids(tenant_url, shop_id, campaign_id=_FBS) == {later_lower_version}

        excluded = await _import(
            tenant_url,
            shop_id=shop_id,
            version=5,
            finished_at=_at(5),
            campaign_id=_FBS,
        )
        await exclude_import(
            tenant_url,
            excluded,
            reason="not suitable",
            basis="explicit test exclusion",
        )
        assert await _selected_ids(tenant_url, shop_id, campaign_id=_FBS) == {later_lower_version}
        assert await lift_import_exclusion(tenant_url, excluded) is True
        assert await _selected_ids(tenant_url, shop_id, campaign_id=_FBS) == {excluded}

        assert await _selected_ids(
            tenant_url, shop_id, campaign_id=_FBY, period=_OCTOBER
        ) == {only}
        september = await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(1),
            period=_SEPTEMBER,
        )
        september_ids = await _selected_ids(tenant_url, shop_id, period=_SEPTEMBER)
        october_ids = await _selected_ids(tenant_url, shop_id, period=_OCTOBER)
        assert september in september_ids
        assert only in october_ids
        assert september_ids.isdisjoint(october_ids)

        foreign = await _import(
            tenant_url,
            shop_id=other_shop_id,
            version=1,
            finished_at=_at(9),
            business_id=99,
        )
        assert foreign in await _selected_ids(tenant_url, other_shop_id)
        assert foreign not in await _selected_ids(tenant_url, shop_id)

        empty_shop = uuid4()
        await _import(
            tenant_url,
            shop_id=empty_shop,
            version=1,
            status="failed",
            finished_at=_at(1),
        )
        assert await _selected_ids(tenant_url, empty_shop) == set()

        group = await _selected_ids(tenant_url, shop_id, campaign_id=_FBS)
        assert len(group) == 1
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(6),
            campaign_id=77,
        )
        tied_later_version = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(6),
            campaign_id=77,
        )
        assert await _selected_ids(tenant_url, shop_id, campaign_id=77) == {tied_later_version}


async def test_new_suitable_version_switches_the_view_without_changing_raw() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = uuid4()
        first = await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(1),
            content=b"PK\x03\x04version-one",
            rows=[("delivered", {"yourSku": "old", "orderId": 1, "deliveredCount": 1})],
        )
        assert await _view_import_ids(tenant_url, "analytics.realization_delivered") == {first}

        second = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            content=b"PK\x03\x04version-two",
            rows=[("delivered", {"yourSku": "new", "orderId": 2, "deliveredCount": 2})],
        )
        after_second = await _raw_fingerprint(tenant_url)
        assert await _view_import_ids(tenant_url, "analytics.realization_delivered") == {second}
        stored = await _raw_fingerprint_for(tenant_url, first)
        await exclude_import(
            tenant_url,
            second,
            reason="hold",
            basis="switch back in the test",
        )
        assert await _view_import_ids(tenant_url, "analytics.realization_delivered") == {first}
        assert await _raw_fingerprint(tenant_url) == after_second
        assert await _raw_fingerprint_for(tenant_url, first) == stored
        assert await _import_status(tenant_url, second) == "done"


async def test_realization_views_keep_trace_sku_and_nulls() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = uuid4()
        business_id = 173963462
        await _campaign(tenant_url, _FBY, business_id, "FBY")
        await _campaign(tenant_url, 41, 8100000041, "FBS")
        import_id = await _import(
            tenant_url,
            shop_id=shop_id,
            business_id=business_id,
            version=1,
            finished_at=_at(1),
            rows=[
                (
                    "delivered",
                    {
                        "yourSku": "гамбургер",
                        "shopSku": "other-sku",
                        "orderId": 61947964226,
                        "deliveredCount": 5,
                        "deliveredPriceSumWithVatAndDiscounts": None,
                    },
                ),
                (
                    "transferred_to_delivery",
                    {
                        "yourSku": "гамбургер",
                        "orderId": 61947964226,
                        "transferredToDeliveryCount": 5,
                    },
                ),
                ("returned", {"yourSku": "коврик", "orderId": 61697494977, "returnedCount": 0}),
                ("returned", {"yourSku": "пустая-сумма", "orderId": 2, "returnedCount": None}),
                ("returned", {"yourSku": "без-количества", "orderId": 3}),
                ("unredeemed", {"yourSku": "невыкуп", "orderId": 4, "unredeemedCount": 1}),
                ("lost_items", {"yourSku": "потеря", "orderId": 5}),
            ],
        )
        delivered = await _rows(tenant_url, "analytics.realization_delivered")
        transferred = await _rows(tenant_url, "analytics.realization_transferred")
        returned = await _rows(tenant_url, "analytics.realization_returned")
        unredeemed = await _rows(tenant_url, "analytics.realization_unredeemed")
        assert len(delivered) == 1
        assert len(transferred) == 1
        assert len(returned) == 3
        assert len(unredeemed) == 1
        assert delivered[0]["import_id"] == import_id
        assert delivered[0]["sheet"] == "delivered"
        assert delivered[0]["row_number"] == 1
        assert delivered[0]["your_sku"] == "гамбургер"
        assert delivered[0]["order_id"] == 61947964226
        assert delivered[0]["placement_type"] == "FBY"
        assert delivered[0]["delivered_count"] == 5
        assert delivered[0]["delivered_price_sum"] is None
        assert "shopSku" not in delivered[0]
        assert transferred[0]["sheet"] == "transferred_to_delivery"
        assert transferred[0]["order_id"] == delivered[0]["order_id"]
        assert transferred[0]["import_id"] == delivered[0]["import_id"]
        assert [row["returned_count"] for row in returned] == [0, None, None]
        definition = await _view_definition(tenant_url, "realization_delivered")
        assert "shopSku" not in definition
        lost_view = await _scalar(
            tenant_url,
            "SELECT to_regclass('analytics.realization_lost_items')",
        )
        assert lost_view is None
        lost_rows = await _scalar(
            tenant_url,
            "SELECT count(*) FROM raw.report_rows WHERE sheet = 'lost_items'",
        )
        assert lost_rows == 1


async def test_october_version_four_is_selected_and_only_the_copy_is_hidden() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = uuid4()
        pair = {"yourSku": "гамбургер", "orderId": 10, "deliveredCount": 2}
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(1),
            rows=[("delivered", dict(pair))],
        )
        version_four = await _import(
            tenant_url,
            shop_id=shop_id,
            version=4,
            finished_at=_at(4),
            rows=[("delivered", dict(pair)), ("delivered", dict(pair))],
        )
        before = await _raw_fingerprint(tenant_url)
        assert await _selected_ids(tenant_url, shop_id, period=_OCTOBER) == {version_four}
        assert await _active_exclusion_count(tenant_url, version_four) == 0
        visible = await _mappings(
            tenant_url,
            """
            SELECT row_number, your_sku
            FROM analytics.realization_delivered
            WHERE import_id = :import_id
            ORDER BY row_number
            """,
            {"import_id": version_four},
        )
        suppressed = await _mappings(
            tenant_url,
            """
            SELECT row_number, kept_row_number, payload
            FROM analytics.realization_suppressed_duplicate
            WHERE import_id = :import_id
            """,
            {"import_id": version_four},
        )
        assert [row["row_number"] for row in visible] == [1]
        assert visible[0]["your_sku"] == "гамбургер"
        assert [row["row_number"] for row in suppressed] == [2]
        assert suppressed[0]["kept_row_number"] == 1
        assert await _raw_fingerprint(tenant_url) == before
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=5,
            status="failed",
            finished_at=_at(5),
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=6,
            status="running",
            finished_at=None,
        )
        assert await _selected_ids(tenant_url, shop_id, period=_OCTOBER) == {version_four}


async def test_datalens_reads_analytics_and_cannot_change_exclusions() -> None:
    async with _tenant() as (admin_url, tenant_url):
        await _ensure_datalens(admin_url)
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = uuid4()
        import_id = await _import(tenant_url, shop_id=shop_id, version=1, finished_at=_at(1))
        assert await _privilege(
            tenant_url,
            "SELECT has_table_privilege("
            "'profitsamurai_datalens', 'analytics.selected_import', 'SELECT')",
        )
        assert await _privilege(
            tenant_url,
            "SELECT has_table_privilege('profitsamurai_datalens', "
            "'analytics.realization_delivered', 'SELECT')",
        )
        assert not await _privilege(
            tenant_url,
            "SELECT has_table_privilege('profitsamurai_datalens', "
            "'analytics.import_exclusions', 'INSERT')",
        )
        assert not await _privilege(
            tenant_url,
            "SELECT has_table_privilege('profitsamurai_datalens', "
            "'analytics.import_exclusions', 'UPDATE')",
        )
        assert not await _privilege(
            tenant_url,
            "SELECT has_schema_privilege('profitsamurai_datalens', 'raw', 'USAGE')",
        )
        assert not await _privilege(
            tenant_url,
            "SELECT has_schema_privilege('profitsamurai_datalens', 'ref', 'USAGE')",
        )
        assert await _import_status(tenant_url, import_id) == "done"


async def test_duplicate_filter_drops_only_adjacent_october_realization_pairs() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = uuid4()
        pair = {"yourSku": "гамбургер", "orderId": 1, "deliveredCount": 5}
        october_v2 = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            rows=[
                ("delivered", pair),
                ("delivered", dict(pair)),
                ("delivered", {"yourSku": "другой", "orderId": 2, "deliveredCount": 1}),
                ("delivered", dict(pair)),
            ],
        )
        september = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            period=_SEPTEMBER,
            rows=[("delivered", dict(pair)), ("delivered", dict(pair))],
        )
        version_one = await _import(
            tenant_url,
            shop_id=uuid4(),
            version=1,
            finished_at=_at(2),
            rows=[("delivered", dict(pair)), ("delivered", dict(pair))],
        )
        services = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            report_type="marketplace_services",
            campaign_id=None,
            rows=[("delivered", dict(pair)), ("delivered", dict(pair))],
        )
        netting = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            report_type="united_netting",
            campaign_id=None,
            rows=[("transaction_date", dict(pair)), ("transaction_date", dict(pair))],
        )
        before = await _raw_fingerprint(tenant_url)
        visible = await _mappings(
            tenant_url,
            """
            SELECT row_number
            FROM analytics.realization_delivered
            WHERE import_id = :import_id
            ORDER BY row_number
            """,
            {"import_id": october_v2},
        )
        suppressed = await _mappings(
            tenant_url,
            """
            SELECT row_number, kept_row_number, reason
            FROM analytics.realization_suppressed_duplicate
            WHERE import_id = :import_id
            ORDER BY row_number
            """,
            {"import_id": october_v2},
        )
        assert [row["row_number"] for row in visible] == [1, 3, 4]
        assert [row["row_number"] for row in suppressed] == [2]
        assert suppressed[0]["kept_row_number"] == 1
        assert "Временная техническая защита" in str(suppressed[0]["reason"])
        assert await _suppressed_count(tenant_url, september) == 0
        assert await _suppressed_count(tenant_url, version_one) == 0
        assert await _suppressed_count(tenant_url, services) == 0
        assert await _suppressed_count(tenant_url, netting) == 0
        assert await _active_exclusion_count(tenant_url, october_v2) == 0
        september_rows = await _mappings(
            tenant_url,
            """
            SELECT row_number
            FROM analytics.realization_delivered
            WHERE import_id = :import_id
            ORDER BY row_number
            """,
            {"import_id": september},
        )
        assert [row["row_number"] for row in september_rows] == [1, 2]
        assert await _raw_fingerprint(tenant_url) == before


def _uuid(value: object) -> UUID:
    assert isinstance(value, UUID)
    return value


def _at(minute: int) -> datetime:
    return datetime(2026, 11, 8, 12, minute, tzinfo=UTC)


async def _import(
    database_url: str,
    *,
    shop_id: UUID,
    version: int,
    finished_at: datetime | None,
    status: str = "done",
    business_id: int = 10,
    report_type: str = "goods_realization",
    campaign_id: int | None = _FBY,
    period: tuple[date, date] = _OCTOBER,
    content: bytes | None = None,
    rows: list[tuple[str, Mapping[str, object]]] | None = None,
) -> UUID:
    import_id = uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO raw.report_imports (
                        id, shop_id, business_id, report_type, period_from, period_to,
                        campaign_id, yandex_report_id, status, version, started_at,
                        finished_at, row_count, error_message
                    ) VALUES (
                        :id, :shop_id, :business_id, :report_type, :period_from, :period_to,
                        :campaign_id, NULL, :status, :version, :started_at,
                        :finished_at, :row_count, NULL
                    )
                    """
                ),
                {
                    "id": import_id,
                    "shop_id": shop_id,
                    "business_id": business_id,
                    "report_type": report_type,
                    "period_from": period[0],
                    "period_to": period[1],
                    "campaign_id": campaign_id,
                    "status": status,
                    "version": version,
                    "started_at": _at(0),
                    "finished_at": finished_at,
                    "row_count": len(rows or []),
                },
            )
            if content is not None:
                await conn.execute(
                    text(
                        """
                        INSERT INTO raw.report_files (
                            id, import_id, content, byte_size, content_sha256
                        ) VALUES (
                            :id, :import_id, :content, :byte_size, :content_sha256
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "import_id": import_id,
                        "content": content,
                        "byte_size": len(content),
                        "content_sha256": hashlib.sha256(content).hexdigest(),
                    },
                )
            for row_number, (sheet, payload) in enumerate(rows or [], start=1):
                await conn.execute(
                    text(
                        """
                        INSERT INTO raw.report_rows (id, import_id, sheet, row_number, payload)
                        VALUES (:id, :import_id, :sheet, :row_number, CAST(:payload AS jsonb))
                        """
                    ),
                    {
                        "id": uuid4(),
                        "import_id": import_id,
                        "sheet": sheet,
                        "row_number": row_number,
                        "payload": json.dumps(payload),
                    },
                )
    finally:
        await engine.dispose()
    return import_id


async def _campaign(
    database_url: str,
    campaign_id: int,
    business_id: int,
    placement_type: str,
) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO ref.campaigns (campaign_id, business_id, placement_type)
                    VALUES (:campaign_id, :business_id, :placement_type)
                    """
                ),
                {
                    "campaign_id": campaign_id,
                    "business_id": business_id,
                    "placement_type": placement_type,
                },
            )
    finally:
        await engine.dispose()


async def _selected_ids(
    database_url: str,
    shop_id: UUID,
    *,
    campaign_id: int | None = None,
    period: tuple[date, date] | None = None,
) -> set[UUID]:
    clauses = ["shop_id = :shop_id"]
    params: dict[str, object] = {"shop_id": shop_id}
    if campaign_id is not None:
        clauses.append("campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if period is not None:
        clauses.append("period_from = :period_from AND period_to = :period_to")
        params["period_from"] = period[0]
        params["period_to"] = period[1]
    rows = await _mappings(
        database_url,
        f"SELECT import_id FROM analytics.selected_import WHERE {' AND '.join(clauses)}",
        params,
    )
    return {_uuid(row["import_id"]) for row in rows}


async def _view_import_ids(database_url: str, view_name: str) -> set[UUID]:
    rows = await _mappings(database_url, f"SELECT import_id FROM {view_name}", {})
    return {_uuid(row["import_id"]) for row in rows}


async def _rows(database_url: str, view_name: str) -> list[dict[str, object]]:
    return await _mappings(
        database_url,
        f"SELECT * FROM {view_name} ORDER BY sheet, row_number",
        {},
    )


async def _view_definition(database_url: str, view_name: str) -> str:
    value = await _scalar(
        database_url,
        "SELECT pg_get_viewdef('analytics.' || :view_name, true)",
        {"view_name": view_name},
    )
    assert isinstance(value, str)
    return value


async def _active_exclusion_count(database_url: str, import_id: UUID) -> int:
    value = await _scalar(
        database_url,
        """
        SELECT count(*)
        FROM analytics.import_exclusions
        WHERE import_id = :import_id AND lifted_at IS NULL
        """,
        {"import_id": import_id},
    )
    assert isinstance(value, int)
    return value


async def _suppressed_count(database_url: str, import_id: UUID) -> int:
    value = await _scalar(
        database_url,
        """
        SELECT count(*)
        FROM analytics.realization_suppressed_duplicate
        WHERE import_id = :import_id
        """,
        {"import_id": import_id},
    )
    assert isinstance(value, int)
    return value


async def _import_status(database_url: str, import_id: UUID) -> str:
    value = await _scalar(
        database_url,
        "SELECT status FROM raw.report_imports WHERE id = :id",
        {"id": import_id},
    )
    assert isinstance(value, str)
    return value


async def _raw_fingerprint(database_url: str) -> tuple[object, ...]:
    row = await _mappings(
        database_url,
        """
        SELECT
          (SELECT count(*) FROM raw.report_imports) AS imports,
          (SELECT coalesce(md5(string_agg(
              status || version::text || coalesce(finished_at::text, ''),
              '' ORDER BY id::text
          )), '') FROM raw.report_imports) AS import_digest,
          (SELECT count(*) FROM raw.report_files) AS files,
          (SELECT coalesce(md5(string_agg(content_sha256, '' ORDER BY id::text)), '')
             FROM raw.report_files) AS file_digest,
          (SELECT coalesce(sum(byte_size), 0) FROM raw.report_files) AS file_bytes,
          (SELECT count(*) FROM raw.report_rows) AS rows,
          (SELECT coalesce(md5(string_agg(md5(payload::text), '' ORDER BY id::text)), '')
             FROM raw.report_rows) AS row_digest
        """,
        {},
    )
    record = row[0]
    return tuple(record[name] for name in record)


async def _raw_fingerprint_for(database_url: str, import_id: UUID) -> tuple[object, ...]:
    row = await _mappings(
        database_url,
        """
        SELECT
          status,
          version,
          coalesce(finished_at::text, '') AS finished_at,
          (SELECT coalesce(content_sha256, '') FROM raw.report_files WHERE import_id = :id)
            AS file_digest,
          (SELECT coalesce(md5(string_agg(md5(payload::text), '' ORDER BY row_number)), '')
             FROM raw.report_rows WHERE import_id = :id) AS row_digest
        FROM raw.report_imports
        WHERE id = :id
        """,
        {"id": import_id},
    )
    record = row[0]
    return tuple(record[name] for name in record)


async def _privilege(database_url: str, statement: str) -> bool:
    value = await _scalar(database_url, statement)
    return bool(value)


async def _ensure_datalens(admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        CREATE ROLE profitsamurai_datalens LOGIN PASSWORD 'it-datalens';
                    EXCEPTION
                        WHEN duplicate_object THEN
                            NULL;
                    END
                    $$
                    """
                )
            )
    finally:
        await engine.dispose()


async def _mappings(
    database_url: str,
    statement: str,
    params: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(statement), params or {})
            return [dict(row) for row in result.mappings()]
    finally:
        await engine.dispose()


async def _scalar(
    database_url: str,
    statement: str,
    params: dict[str, object] | None = None,
) -> object:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(statement), params or {})
    finally:
        await engine.dispose()
