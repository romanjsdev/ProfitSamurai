import hashlib
import io
import json
import logging
import zipfile
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import cast
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from profitsamurai.api.schemas.finance import FinanceImportCreate
from profitsamurai.finance import (
    FinanceImportBusy,
    advisory_keys,
    hold_import_lock,
    import_reports,
    public_error,
)
from profitsamurai.finance_recovery import classify_running_import
from profitsamurai.integrations.yandex_market.client import (
    YandexMarketClient,
    rate_limit_wait_seconds,
)
from profitsamurai.integrations.yandex_market.errors import YandexMarketError
from profitsamurai.integrations.yandex_market.reports import (
    ReportDownloadError,
    ReportParseError,
    ShopCampaign,
    build_report_requests,
    download_report_file,
    interpret_report_info,
    parse_report_bytes,
    poll_report,
)
from profitsamurai.jsonutil import JsonObject

PERIOD_FROM = date(2026, 10, 1)
PERIOD_TO = date(2026, 10, 31)
FBY = ShopCampaign(129958161, "FBY")
FBS = ShopCampaign(148633332, "FBS")
SHOP_ID = UUID("7fa5a7ae-7619-407b-a599-c5456571750d")
API_KEY = "super-secret-api-key"
SIGNED_URL = "https://cdn.example/report.zip?sig=signed-secret"


def test_requests_cover_realization_services_and_netting() -> None:
    requests = build_report_requests(
        business_id=173963462,
        campaigns=[FBS, FBY],
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
    )
    assert [item.report_type for item in requests] == [
        "goods_realization",
        "goods_realization",
        "marketplace_services",
        "united_netting",
    ]
    assert [item.campaign_id for item in requests[:2]] == [129958161, 148633332]
    assert requests[0].body == {"campaignId": 129958161, "year": 2026, "month": 10}
    assert requests[1].body == {"campaignId": 148633332, "year": 2026, "month": 10}
    assert requests[0].params == {"format": "JSON"}
    services = requests[2].body
    assert services["businessId"] == 173963462
    assert services["dateFrom"] == "2026-10-01"
    assert services["dateTo"] == "2026-10-31"
    assert services["campaignIds"] == [129958161, 148633332]
    assert services["placementPrograms"] == ["FBS", "FBY"]
    netting = requests[3].body
    assert netting["businessId"] == 173963462
    assert netting["campaignIds"] == [129958161, 148633332]
    assert "placementPrograms" not in netting
    for item in requests:
        assert "dateTimeFrom" not in item.body
        assert "dateTimeTo" not in item.body
        assert item.path.startswith("/v2/reports/")


def test_realization_month_comes_from_the_period() -> None:
    requests = build_report_requests(
        business_id=10,
        campaigns=[ShopCampaign(7, "FBS")],
        period_from=date(2024, 3, 1),
        period_to=date(2024, 3, 31),
    )
    assert requests[0].body == {"campaignId": 7, "year": 2024, "month": 3}


def test_period_must_be_one_calendar_month() -> None:
    with pytest.raises(ValidationError, match="calendar month"):
        FinanceImportCreate(period_from=date(2026, 10, 2), period_to=date(2026, 10, 31))


def test_report_statuses() -> None:
    waiting = interpret_report_info(_info("PROCESSING"), "rep-1")
    assert waiting.kind == "wait"
    ready = interpret_report_info(_info("DONE", file_url=SIGNED_URL), "rep-1")
    assert ready.kind == "done"
    assert ready.file_url == SIGNED_URL
    assert interpret_report_info(_info("DONE", sub_status="NO_DATA"), "rep-1").kind == "no_data"
    too_large = interpret_report_info(_info("FAILED", sub_status="TOO_LARGE"), "rep-1")
    assert too_large.kind == "too_large"
    assert interpret_report_info(_info("FAILED"), "rep-1").kind == "failed"
    missing = interpret_report_info(_info("FAILED", sub_status="RESOURCE_NOT_FOUND"), "rep-1")
    assert missing.kind == "not_found"


async def test_polling_stops_on_done_and_on_timeout() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        status = "PENDING" if calls["count"] == 1 else "DONE"
        return httpx.Response(200, json=_info(status, file_url=SIGNED_URL))

    async with YandexMarketClient(
        api_key=API_KEY,
        base_url="https://api.partner.market.yandex.ru",
        transport=httpx.MockTransport(handler),
        attempts=1,
    ) as client:
        state = await poll_report(
            client,
            "rep-1",
            timeout_seconds=5,
            pause_seconds=0,
            sleep=_no_sleep,
        )
    assert state.kind == "done"
    assert calls["count"] == 2

    def pending(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_info("PENDING"))

    async with YandexMarketClient(
        api_key=API_KEY,
        base_url="https://api.partner.market.yandex.ru",
        transport=httpx.MockTransport(pending),
        attempts=1,
    ) as client:
        with pytest.raises(Exception, match="timeout"):
            await poll_report(
                client,
                "rep-1",
                timeout_seconds=0,
                pause_seconds=0,
                sleep=_no_sleep,
            )


async def test_download_does_not_send_the_api_key_and_rejects_bad_urls() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.extend(request.headers.keys())
        assert str(request.url) == "https://cdn.example/report.zip"
        return httpx.Response(200, content=b"{}")

    body = await download_report_file(
        "https://cdn.example/report.zip",
        transport=httpx.MockTransport(handler),
    )
    assert body == b"{}"
    assert "api-key" not in {name.lower() for name in seen}

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("download was attempted")

    with pytest.raises(ReportDownloadError, match="rejected"):
        await download_report_file(
            "http://cdn.example/report.zip",
            transport=httpx.MockTransport(fail),
        )
    with pytest.raises(ReportDownloadError, match="rejected"):
        await download_report_file(
            "https://user:password@cdn.example/report.zip",
            transport=httpx.MockTransport(fail),
        )
    with pytest.raises(ReportDownloadError, match="rejected"):
        await download_report_file(
            "https://127.0.0.1/report.zip",
            transport=httpx.MockTransport(fail),
        )
    with pytest.raises(ReportDownloadError, match="not downloaded"):
        await download_report_file(
            "https://cdn.example/report.zip",
            max_bytes=3,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"12345")),
        )


def test_parser_keeps_json_rows_from_a_zip() -> None:
    content = _zip({"delivered.json": [{"sku": "a", "amount": 1}], "empty.json": {"rows": []}})
    rows = parse_report_bytes(content)
    assert rows == [("delivered", {"sku": "a", "amount": 1})]


def test_parser_keeps_exact_duplicate_rows() -> None:
    content = _zip({"delivered.json": [{"sku": "a"}, {"sku": "a"}]})
    rows = parse_report_bytes(content)
    assert rows == [("delivered", {"sku": "a"}), ("delivered", {"sku": "a"})]


def test_parser_rejects_a_non_object_and_names_the_file_and_position() -> None:
    content = _zip({"delivered.json": [{"sku": "a"}, "secret-token", {"sku": "b"}]})
    with pytest.raises(ReportParseError, match="delivered.json element 2") as caught:
        parse_report_bytes(content)
    assert "secret-token" not in str(caught.value)


async def test_import_saves_file_rows_and_a_new_version(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    archive = _zip({"sheet.json": [{"sku": "a"}]})
    store = _MemoryStore()
    first = await _import(store, archive)
    second = await _import(store, archive)
    assert first.version == 1
    assert second.version == 2
    assert len(store.files) == 8
    assert all(content == archive for content in store.files.values())
    saved_rows = [rows for rows in store.rows.values() if rows]
    assert saved_rows[0] == [(1, "sheet", {"sku": "a"})]
    assert API_KEY not in caplog.text
    assert "signed-secret" not in caplog.text


async def test_one_failed_report_does_not_stop_the_others() -> None:
    archive = _zip({"sheet.json": [{"sku": "a"}]})

    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("goods-realization/generate"):
            body = json.loads(request.content)
            if body["campaignId"] == 129958161:
                return httpx.Response(403, json={"status": "ERROR"})
        if request.method == "POST":
            return httpx.Response(200, json={"status": "OK", "result": {"reportId": "rep-ok"}})
        if request.url.path.endswith("rep-ok"):
            return httpx.Response(200, json=_info("DONE", sub_status="NO_DATA"))
        raise AssertionError(request.url.path)

    store = _MemoryStore()
    result = await import_reports(
        api_key=API_KEY,
        base_url="https://api.partner.market.yandex.ru",
        business_id=173963462,
        shop_id=SHOP_ID,
        campaigns=[FBY, FBS],
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        store=store,
        transport=httpx.MockTransport(api),
        download_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=archive)
        ),
        timeout_seconds=1,
        pause_seconds=0,
        gap_seconds=0,
        sleep=_no_sleep,
        attempts=1,
    )
    statuses = [item.status for item in result.reports]
    assert statuses[0] == "failed"
    assert result.reports[0].error_message == "partner api error 403"
    assert statuses[1:] == ["no_data", "no_data", "no_data"]
    assert API_KEY not in str(result.reports[0].error_message)


async def test_network_error_and_secret_do_not_leak(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    def api(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {API_KEY} {SIGNED_URL}")

    store = _MemoryStore()
    result = await _import(store, b"{}", api=api)
    assert result.reports[0].error_message == "partner api connection failed"
    assert API_KEY not in caplog.text
    assert "signed-secret" not in caplog.text
    leaked = RuntimeError(f"boom {API_KEY} {SIGNED_URL}")
    assert public_error(leaked) == "finance import failed"
    assert API_KEY not in public_error(YandexMarketError("partner api rejected the request", 502))


def test_rate_limit_delay_prefers_the_later_header() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    until = format_datetime(now + timedelta(seconds=90))
    assert rate_limit_wait_seconds(httpx.Headers({"Retry-After": "7"}), fallback=120, now=now) == 7
    resource = rate_limit_wait_seconds(
        httpx.Headers({"X-RateLimit-Resource-Until": until}),
        fallback=1,
        now=now,
    )
    assert resource == pytest.approx(90)
    both = rate_limit_wait_seconds(
        httpx.Headers({"Retry-After": "5", "X-RateLimit-Resource-Until": until}),
        fallback=1,
        now=now,
    )
    assert both == pytest.approx(90)
    assert rate_limit_wait_seconds(httpx.Headers(), fallback=120, now=now) == 120


async def test_same_method_waits_two_minutes_and_a_rejected_generate_is_repeated() -> None:
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    posts = {"limited": 0, "total": 0}

    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["total"] += 1
            body = json.loads(request.content)
            if body.get("campaignId") == 129958161:
                posts["limited"] += 1
                if posts["limited"] == 1:
                    return httpx.Response(
                        429,
                        headers={"Retry-After": "7"},
                        json={"status": "ERROR"},
                    )
            return httpx.Response(200, json={"status": "OK", "result": {"reportId": "rep-1"}})
        return httpx.Response(200, json=_info("DONE", file_url="https://cdn.example/report.zip"))

    await import_reports(
        api_key=API_KEY,
        base_url="https://api.partner.market.yandex.ru",
        business_id=173963462,
        shop_id=SHOP_ID,
        campaigns=[FBY, FBS],
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        store=_MemoryStore(),
        transport=httpx.MockTransport(api),
        download_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"{}")),
        timeout_seconds=1,
        pause_seconds=0,
        gap_seconds=120,
        sleep=record,
        attempts=3,
    )
    assert posts["limited"] == 2
    assert 7 in pauses
    assert any(pause == pytest.approx(120) for pause in pauses)


async def test_a_lost_generate_response_is_not_posted_again() -> None:
    posts = {"count": 0}

    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["count"] += 1
            raise httpx.ConnectError("timed out")
        raise AssertionError(request.url.path)

    result = await import_reports(
        api_key=API_KEY,
        base_url="https://api.partner.market.yandex.ru",
        business_id=173963462,
        shop_id=SHOP_ID,
        campaigns=[FBY, FBS],
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        store=_MemoryStore(),
        transport=httpx.MockTransport(api),
        timeout_seconds=1,
        pause_seconds=0,
        gap_seconds=0,
        sleep=_no_sleep,
        attempts=3,
    )
    assert posts["count"] == 1
    assert result.reports[0].error_message == "partner api connection failed"
    assert [item.error_message for item in result.reports[1:]] == [
        "previous report generation result is unknown"
    ] * 3


def test_a_running_import_is_abandoned_only_when_the_shop_lock_is_free() -> None:
    assert classify_running_import(shop_lock_held=False) == "abandoned"
    assert classify_running_import(shop_lock_held=True) == "active_or_unidentified"


async def test_finalize_failure_keeps_the_saved_row_count() -> None:
    archive = _zip({"sheet.json": [{"sku": "a"}, {"sku": "a"}]})
    store = _MemoryStore()
    store.fail_next_finish = True
    result = await _import(store, archive, campaigns=[FBY])
    assert result.reports[0].status == "failed"
    assert result.reports[0].error_message == "report finalize failed"
    assert result.reports[0].row_count == 2
    assert store.finish_calls[:2] == [("done", 2), ("failed", 2)]
    assert ("failed", 0) not in store.finish_calls
    failed = next(item for item in store.items.values() if item.status == "failed")
    assert failed.row_count == 2
    assert store.rows[next(key for key, item in store.items.items() if item is failed)] == [
        (1, "sheet", {"sku": "a"}),
        (2, "sheet", {"sku": "a"}),
    ]


async def test_unrecorded_finalize_failure_does_not_zero_the_count() -> None:
    archive = _zip({"sheet.json": [{"sku": "a"}]})
    store = _MemoryStore()
    store.fail_all_finishes = True
    result = await _import(store, archive, campaigns=[FBY])
    assert result.reports[0].status == "running"
    assert result.reports[0].row_count == 1
    assert result.reports[0].error_message == "report finalize failed"
    assert all(count == 1 for _status, count in store.finish_calls)
    assert store.items[UUID(int=1)].status == "running"
    assert store.rows[UUID(int=1)] == [(1, "sheet", {"sku": "a"})]


async def test_download_parse_and_save_failures_stay_distinct() -> None:
    def reject_download(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404)

    store = _MemoryStore()
    downloaded = await _import(
        store,
        b"{}",
        campaigns=[FBY],
        download=reject_download,
    )
    assert downloaded.reports[0].status == "failed"
    assert downloaded.reports[0].error_message == (
        "report download failed: report file was not downloaded"
    )
    assert downloaded.reports[0].row_count == 0
    assert store.files == {}

    parsed = _MemoryStore()
    bad = _zip({"delivered.json": [{"sku": "a"}, 5]})
    parse_result = await _import(parsed, bad, campaigns=[FBY])
    assert parse_result.reports[0].status == "failed"
    assert parse_result.reports[0].error_message is not None
    assert "delivered.json element 2" in parse_result.reports[0].error_message
    assert parse_result.reports[0].row_count == 0
    assert bad in parsed.files.values()
    assert parsed.rows == {}

    saving = _MemoryStore()
    saving.fail_rows = True
    archive = _zip({"sheet.json": [{"sku": "a"}]})
    save_result = await _import(saving, archive, campaigns=[FBY])
    assert save_result.reports[0].status == "failed"
    assert save_result.reports[0].error_message == "report row save failed"
    assert save_result.reports[0].row_count == 0
    assert archive in saving.files.values()
    assert saving.rows == {}


async def test_a_new_version_keeps_the_previous_file_hash() -> None:
    first_archive = _zip({"sheet.json": [{"sku": "a"}]})
    second_archive = _zip({"sheet.json": [{"sku": "b"}]})
    store = _MemoryStore()
    await _import(store, first_archive, campaigns=[FBY])
    first_hash = hashlib.sha256(first_archive).hexdigest()
    second_hash = hashlib.sha256(second_archive).hexdigest()
    assert set(store.hashes.values()) == {first_hash}
    snapshot = dict(store.hashes)
    await _import(store, second_archive, campaigns=[FBY])
    for import_id, digest in snapshot.items():
        assert store.hashes[import_id] == digest == first_hash
    assert second_hash in store.hashes.values()
    assert first_hash != second_hash


async def test_parallel_import_of_the_same_shop_period_is_rejected() -> None:
    connection = _OneConnection()

    async def first() -> None:
        locked = cast(AsyncConnection, connection)
        async with hold_import_lock(locked, 1, 2):
            connection.entered = True
            with pytest.raises(FinanceImportBusy, match="already running"):
                async with hold_import_lock(locked, 1, 2):
                    pass

    await first()
    assert connection.unlocks == 1
    assert advisory_keys(SHOP_ID) == advisory_keys(SHOP_ID)
    other_shop = UUID("11111111-1111-1111-1111-111111111111")
    assert advisory_keys(other_shop) != advisory_keys(SHOP_ID)


def _info(status: str, sub_status: str | None = None, file_url: str | None = None) -> JsonObject:
    result: JsonObject = {"status": status}
    if sub_status is not None:
        result["subStatus"] = sub_status
    if file_url is not None:
        result["file"] = file_url
    return {"status": "OK", "result": result}


def _zip(files: dict[str, object]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, json.dumps(payload))
    return buffer.getvalue()


async def _no_sleep(_seconds: float) -> None:
    return None


class _Saved:
    def __init__(
        self,
        shop_id: UUID,
        period_from: date,
        period_to: date,
        report_type: str,
        campaign_id: int | None,
        version: int,
    ) -> None:
        self.shop_id = shop_id
        self.period_from = period_from
        self.period_to = period_to
        self.report_type = report_type
        self.campaign_id = campaign_id
        self.version = version
        self.status = "running"
        self.row_count = 0
        self.error_message: str | None = None
        self.yandex_report_id: str | None = None


class _MemoryStore:
    def __init__(self) -> None:
        self.items: dict[UUID, _Saved] = {}
        self.files: dict[UUID, bytes] = {}
        self.hashes: dict[UUID, str] = {}
        self.rows: dict[UUID, list[tuple[int, str, JsonObject]]] = {}
        self.finish_calls: list[tuple[str, int]] = []
        self.fail_next_finish = False
        self.fail_all_finishes = False
        self.fail_rows = False

    async def next_version(self, shop_id: UUID, period_from: date, period_to: date) -> int:
        found = [
            item.version
            for item in self.items.values()
            if item.shop_id == shop_id
            and item.period_from == period_from
            and item.period_to == period_to
        ]
        return max(found, default=0) + 1

    async def begin(
        self,
        *,
        shop_id: UUID,
        business_id: int,
        report_type: str,
        period_from: date,
        period_to: date,
        campaign_id: int | None,
        version: int,
    ) -> UUID:
        import_id = UUID(int=len(self.items) + 1)
        self.items[import_id] = _Saved(
            shop_id,
            period_from,
            period_to,
            report_type,
            campaign_id,
            version,
        )
        return import_id

    async def save_file(self, import_id: UUID, content: bytes) -> None:
        self.files[import_id] = content
        self.hashes[import_id] = hashlib.sha256(content).hexdigest()

    async def save_rows(self, import_id: UUID, rows: list[tuple[str, JsonObject]]) -> None:
        if self.fail_rows:
            raise RuntimeError("rows were not saved")
        self.rows[import_id] = [
            (index, sheet, payload) for index, (sheet, payload) in enumerate(rows, start=1)
        ]

    async def finish(
        self,
        import_id: UUID,
        *,
        status: str,
        row_count: int,
        error_message: str | None,
        yandex_report_id: str | None,
    ) -> None:
        self.finish_calls.append((status, row_count))
        if self.fail_all_finishes or self.fail_next_finish:
            self.fail_next_finish = False
            raise RuntimeError("finish failed")
        item = self.items[import_id]
        item.status = status
        item.row_count = row_count
        item.error_message = error_message
        item.yandex_report_id = yandex_report_id


class _OneConnection:
    def __init__(self) -> None:
        self.held = False
        self.unlocks = 0
        self.entered = False

    async def scalar(self, statement: object, params: dict[str, int]) -> bool:
        assert "pg_try_advisory_lock" in str(statement)
        if self.held:
            return False
        self.held = True
        return True

    async def execute(self, statement: object, params: dict[str, int]) -> None:
        assert "pg_advisory_unlock" in str(statement)
        self.held = False
        self.unlocks += 1


async def _import(
    store: _MemoryStore,
    archive: bytes,
    api: object | None = None,
    campaigns: list[ShopCampaign] | None = None,
    download: object | None = None,
):
    def default_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Api-Key"] == API_KEY
        if request.method == "POST":
            return httpx.Response(200, json={"status": "OK", "result": {"reportId": "rep-1"}})
        return httpx.Response(200, json=_info("DONE", file_url="https://cdn.example/report.zip"))

    handler = default_api if api is None else api

    def default_download(request: httpx.Request) -> httpx.Response:
        assert "api-key" not in {name.lower() for name in request.headers}
        return httpx.Response(200, content=archive)

    download_handler = default_download if download is None else download

    return await import_reports(
        api_key=API_KEY,
        base_url="https://api.partner.market.yandex.ru",
        business_id=173963462,
        shop_id=SHOP_ID,
        campaigns=[FBY, FBS] if campaigns is None else campaigns,
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        store=store,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        download_transport=httpx.MockTransport(download_handler),  # type: ignore[arg-type]
        timeout_seconds=1,
        pause_seconds=0,
        gap_seconds=0,
        sleep=_no_sleep,
        attempts=1,
    )
