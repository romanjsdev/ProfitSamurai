"""Service and netting views against a disposable PostgreSQL.

Set RUN_INTEGRATION=1, INTEGRATION_DATABASE_URL, and INTEGRATION_ADMIN_URL.
The URLs are checked before any connection. The working cluster is rejected.
"""

import asyncio
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from .test_analytics_selection import _import, _raw_fingerprint
from .test_finance_reliability import _migrate, _tenant

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to use a disposable database",
)

_SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
_OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def _at(minute: int) -> datetime:
    return datetime(2026, 11, 10, 12, minute, tzinfo=UTC)


async def test_service_views_follow_the_selected_import_and_split_by_sku() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = UUID("36ccdadf-995d-41aa-a746-52c8218df2c5")
        older = await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(1),
            report_type="marketplace_services",
            campaign_id=None,
            period=_OCTOBER,
            rows=[("placement", {"shopSku": "old", "orderId": 1, "totalAmount": 1})],
        )
        chosen = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            report_type="marketplace_services",
            campaign_id=None,
            period=_OCTOBER,
            content=b"services-v2",
            rows=_service_rows(),
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=3,
            finished_at=_at(3),
            status="failed",
            report_type="marketplace_services",
            campaign_id=None,
            period=_OCTOBER,
            rows=[("placement", {"shopSku": "ghost", "orderId": 9, "totalAmount": 1000000})],
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=4,
            finished_at=None,
            status="running",
            report_type="marketplace_services",
            campaign_id=None,
            period=_OCTOBER,
            rows=[("placement", {"shopSku": "running", "orderId": 8, "totalAmount": 1000000})],
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(1),
            report_type="marketplace_services",
            campaign_id=None,
            period=_SEPTEMBER,
            rows=[("boost", {"shopSku": "sep", "orderId": 7, "servicePrice": 2})],
        )
        before = await _raw_fingerprint(tenant_url)

        sku = await _rows(tenant_url, "analytics.service_sku_charge")
        order = await _rows(tenant_url, "analytics.service_order_charge")
        assert {row["import_id"] for row in sku + order}.isdisjoint({older})
        assert "ghost" not in _values(sku, "shop_sku")
        assert "running" not in _values(sku, "shop_sku")
        assert "old" not in _values(sku, "shop_sku")
        october_sku = [row for row in sku if row["import_id"] == chosen]
        october_order = [row for row in order if row["import_id"] == chosen]
        by_sheet = {str(row["sheet"]): row for row in october_sku if row["sheet"] != "boost"}
        boosts = [row for row in october_sku if row["sheet"] == "boost"]
        assert len(boosts) == 2
        assert by_sheet["placement"]["amount"] == Decimal("10.5")
        assert by_sheet["placement"]["amount_field"] == "totalAmount"
        assert by_sheet["placement"]["shop_sku"] == "sku-1"
        assert by_sheet["placement"]["service_datetime"] == "2026-09-25 15:26:43"
        assert by_sheet["placement"]["service_date"] is None
        assert by_sheet["placement"]["currency"] == "RUB"
        assert by_sheet["placement"]["partner_id"] == 173963461
        assert by_sheet["placement"]["campaign_id"] is None
        assert by_sheet["paid_storage_after_01-09-26"]["amount"] == Decimal("1.5")
        assert by_sheet["paid_storage_after_01-09-26"]["order_id"] is None
        assert by_sheet["future_sheet"]["amount"] is None
        assert by_sheet["future_sheet"]["amount_field"] is None
        assert {row["shop_sku"] for row in boosts} == {"sku-2"}
        assert boosts[0]["service_datetime"] is None
        assert boosts[0]["service_date"] == "2026-09-01"
        orders = {str(row["sheet"]): row for row in october_order}
        assert orders["order_processing"]["amount"] == Decimal("4")
        assert orders["order_processing"]["shop_sku"] is None
        assert orders["order_processing"]["order_id"] == 200
        assert orders["storage_of_returns"]["shop_sku"] is None
        assert orders["storage_of_returns"]["order_id"] == 201
        assert orders["unassigned"]["order_id"] is None
        assert orders["unassigned"]["shop_sku"] is None
        assert orders["unassigned"]["amount"] is None
        file_id = await _scalar(
            tenant_url,
            "SELECT id FROM raw.report_files WHERE import_id = :import_id",
            {"import_id": chosen},
        )
        assert by_sheet["placement"]["file_id"] == file_id
        assert by_sheet["placement"]["import_id"] == chosen
        assert by_sheet["placement"]["row_number"] == 1
        assert "not-a-shop-sku" not in _values(sku, "shop_sku")
        assert await _raw_fingerprint(tenant_url) == before


async def test_netting_keeps_null_ids_and_bank_sum_is_not_repeated() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        shop_id = UUID("36ccdadf-995d-41aa-a746-52c8218df2c5")
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=1,
            finished_at=_at(1),
            report_type="united_netting",
            campaign_id=None,
            period=_OCTOBER,
            rows=[("transaction_date", {"transactionSum": 9999, "bankOrderId": 1, "bankSum": 1})],
        )
        chosen = await _import(
            tenant_url,
            shop_id=shop_id,
            version=2,
            finished_at=_at(2),
            report_type="united_netting",
            campaign_id=None,
            period=_OCTOBER,
            content=b"netting-v2",
            rows=_netting_rows(),
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=3,
            finished_at=_at(3),
            status="failed",
            report_type="united_netting",
            campaign_id=None,
            period=_OCTOBER,
            rows=[("transaction_date", {"bankOrderId": 10, "bankSum": 1, "transactionSum": 1})],
        )
        await _import(
            tenant_url,
            shop_id=shop_id,
            version=4,
            finished_at=None,
            status="running",
            report_type="united_netting",
            campaign_id=None,
            period=_OCTOBER,
            rows=[("transaction_date", {"bankOrderId": 10, "bankSum": 1, "transactionSum": 1})],
        )
        before = await _raw_fingerprint(tenant_url)

        movements = await _rows(tenant_url, "analytics.netting_movement")
        orders = await _rows(tenant_url, "analytics.bank_order")
        conflicts = await _rows(tenant_url, "analytics.bank_order_conflict")
        assert {row["import_id"] for row in movements} == {chosen}
        assert len(movements) == 5
        assert all(row["transaction_id"] is None for row in movements)
        assert sum(row["bank_order_id"] is None for row in movements) == 1
        assert sum(row["shop_sku"] is None for row in movements) == 1
        assert sum(row["order_id"] is None for row in movements) == 1
        assert _money(movements, "bank_sum") == Decimal("310")
        assert _money(orders, "bank_sum") == Decimal("100")
        by_order = {int(str(row["bank_order_id"])): row for row in orders}
        assert set(by_order) == {10, 11}
        assert by_order[10]["bank_sum"] == Decimal("100")
        assert by_order[10]["bank_order_date"] == "2026-10-01"
        assert by_order[10]["payment_status"] is None
        assert by_order[10]["movement_count"] == 2
        assert by_order[11]["bank_sum"] is None
        assert by_order[11]["bank_order_date"] is None
        assert by_order[11]["payment_status"] == "A"
        found = {(row["bank_order_id"], row["conflict_kind"]) for row in conflicts}
        assert found == {(10, "payment_status"), (11, "bank_sum"), (11, "bank_order_date")}
        premium = next(row for row in movements if row["bank_order_id"] is None)
        assert premium["transaction_sum"] == Decimal("883.32")
        assert premium["row_number"] == 5
        assert premium["file_id"] is not None
        assert await _raw_fingerprint(tenant_url) == before


def _service_rows() -> list[tuple[str, Mapping[str, object]]]:
    boost = {
        "shopSku": "sku-2",
        "orderId": 101,
        "servicePrice": 3,
        "serviceDate": "2026-09-01",
        "actDate": "2026-09-30",
        "serviceName": "Буст",
    }
    return [
        (
            "placement",
            {
                "shopSku": "sku-1",
                "orderId": 100,
                "partnerId": 173963461,
                "serviceName": "Размещение",
                "serviceDateTime": "2026-09-25 15:26:43",
                "actDate": "2026-09-30",
                "totalAmount": 10.5,
                "price": 99,
                "placementModel": "FBY",
                "yourSku": "other",
                "currency": "RUB",
            },
        ),
        ("boost", boost),
        ("boost", boost),
        (
            "paid_storage_after_01-09-26",
            {"shopSku": "sku-3", "paidStorage": 1.5, "serviceDate": "2026-09-02"},
        ),
        (
            "order_processing",
            {
                "orderId": 200,
                "servicePrice": 4,
                "yourSku": "not-a-shop-sku",
                "serviceDate": "2026-09-03",
            },
        ),
        (
            "storage_of_returns",
            {"shopSku": "  ", "orderId": 201, "servicePrice": 5},
        ),
        ("unassigned", {"serviceName": "Без ключа", "servicePrice": 7}),
        ("future_sheet", {"shopSku": "sku-x", "servicePrice": 8, "totalAmount": 9}),
    ]


def _netting_rows() -> list[tuple[str, Mapping[str, object]]]:
    return [
        (
            "transaction_date",
            {
                "transactionId": None,
                "bankOrderId": 10,
                "bankSum": 100,
                "bankOrderDate": "2026-10-01",
                "paymentStatus": "A",
                "transactionSum": 1,
                "orderId": 1,
                "shopSku": "a",
            },
        ),
        (
            "transaction_date",
            {
                "transactionId": None,
                "bankOrderId": 10,
                "bankSum": 100,
                "bankOrderDate": "2026-10-01",
                "paymentStatus": "B",
                "transactionSum": 2,
                "orderId": 2,
                "shopSku": "b",
            },
        ),
        (
            "transaction_date",
            {
                "transactionId": None,
                "bankOrderId": 11,
                "bankSum": 50,
                "bankOrderDate": "2026-10-02",
                "paymentStatus": "A",
                "transactionSum": 3,
                "orderId": 3,
                "shopSku": "c",
            },
        ),
        (
            "transaction_date",
            {
                "transactionId": None,
                "bankOrderId": 11,
                "bankSum": 60,
                "bankOrderDate": "2026-10-03",
                "paymentStatus": "A",
                "transactionSum": 4,
                "orderId": 4,
                "shopSku": "d",
            },
        ),
        (
            "transaction_date",
            {
                "transactionId": None,
                "transactionSum": 883.32,
                "paymentStatus": "Премия",
            },
        ),
    ]


def _values(rows: list[dict[str, object]], name: str) -> set[object]:
    return {row[name] for row in rows}


def _money(rows: list[dict[str, object]], name: str) -> Decimal:
    total = Decimal(0)
    for row in rows:
        value = row[name]
        if value is not None:
            assert isinstance(value, Decimal)
            total += value
    return total


async def _rows(database_url: str, view_name: str) -> list[dict[str, object]]:
    from .test_analytics_selection import _mappings

    return await _mappings(database_url, f"SELECT * FROM {view_name}", {})


async def _scalar(
    database_url: str,
    statement: str,
    params: dict[str, object],
) -> object:
    from .test_analytics_selection import _scalar as read_scalar

    return await read_scalar(database_url, statement, params)
