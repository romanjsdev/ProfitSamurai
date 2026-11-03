"""Yandex Market financial report requests, status, and file download."""

import io
import ipaddress
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import httpx

from profitsamurai.integrations.yandex_market.client import YandexMarketClient
from profitsamurai.jsonutil import (
    JsonObject,
    JsonValue,
    as_object_list,
    expect_json,
    loads,
    nested_object,
    pick_str,
)

_REPORT_ID = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_MAX_ARCHIVE_FILES = 50
_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
# Each generate method publishes its own resource limit. Without a Medium
# subscription that limit is one request per two minutes. The three methods do
# not share one window. A Medium subscription is not assumed.
GENERATION_INTERVAL_SECONDS = 120.0

Sleep = Callable[[float], Awaitable[None]]


class ReportDownloadError(Exception):
    """The file was not saved. The message is safe to show."""


class ReportParseError(Exception):
    """The original file is saved, but its rows could not be read."""


class ReportTimeout(Exception):
    """Generation did not finish within the allotted time."""


@dataclass(frozen=True)
class ShopCampaign:
    campaign_id: int
    placement_type: str | None


@dataclass(frozen=True)
class ReportRequest:
    report_type: str
    path: str
    params: dict[str, str]
    body: JsonObject
    campaign_id: int | None


@dataclass(frozen=True)
class ReportPoll:
    kind: str
    file_url: str | None
    yandex_report_id: str


def generation_interval_seconds(path: str) -> float:
    """Minimum delay before repeating one generate method without a Medium subscription.

    Goods realization, marketplace services, and united netting each publish
    one request per two minutes. The interval applies to a repeated call of the
    same method. A Medium subscription is not assumed.
    """
    known = (
        "/v2/reports/goods-realization/generate",
        "/v2/reports/united-marketplace-services/generate",
        "/v2/reports/united-netting/generate",
    )
    if path not in known:
        return GENERATION_INTERVAL_SECONDS
    return GENERATION_INTERVAL_SECONDS


def require_calendar_month(period_from: date, period_to: date) -> None:
    """Goods realization is ordered by a calendar month, not an arbitrary range."""
    same_month = (period_from.year, period_from.month) == (period_to.year, period_to.month)
    if period_from.day != 1 or not same_month:
        raise ValueError("period must be one calendar month")
    last_day = _last_day(period_from.year, period_from.month)
    if period_to.day != last_day:
        raise ValueError("period must be one calendar month")


def build_report_requests(
    *,
    business_id: int,
    campaigns: list[ShopCampaign],
    period_from: date,
    period_to: date,
) -> list[ReportRequest]:
    require_calendar_month(period_from, period_to)
    ordered = sorted(campaigns, key=lambda item: item.campaign_id)
    if not ordered:
        raise ValueError("shop has no campaigns")
    date_from = period_from.isoformat()
    date_to = period_to.isoformat()
    campaign_ids: list[JsonValue] = [item.campaign_id for item in ordered]
    programs: list[JsonValue] = []
    for placement in sorted({item.placement_type for item in ordered if item.placement_type}):
        programs.append(placement)
    requests = [
        ReportRequest(
            report_type="goods_realization",
            path="/v2/reports/goods-realization/generate",
            params={"format": "JSON"},
            body={
                "campaignId": item.campaign_id,
                "year": period_from.year,
                "month": period_from.month,
            },
            campaign_id=item.campaign_id,
        )
        for item in ordered
    ]
    services: JsonObject = {
        "businessId": business_id,
        "dateFrom": date_from,
        "dateTo": date_to,
        "campaignIds": campaign_ids,
    }
    if programs:
        services["placementPrograms"] = programs
    requests.append(
        ReportRequest(
            report_type="marketplace_services",
            path="/v2/reports/united-marketplace-services/generate",
            params={"format": "JSON"},
            body=services,
            campaign_id=None,
        )
    )
    requests.append(
        ReportRequest(
            report_type="united_netting",
            path="/v2/reports/united-netting/generate",
            params={"format": "JSON"},
            body={
                "businessId": business_id,
                "dateFrom": date_from,
                "dateTo": date_to,
                "campaignIds": campaign_ids,
            },
            campaign_id=None,
        )
    )
    return requests


def report_id_from(payload: JsonObject) -> str:
    source = nested_object(payload, "result")
    body = source if source is not None else payload
    report_id = pick_str(body, "reportId")
    if report_id is None or _REPORT_ID.fullmatch(report_id) is None:
        raise ReportDownloadError("report id was missing")
    return report_id


def interpret_report_info(payload: JsonObject, report_id: str) -> ReportPoll:
    source = nested_object(payload, "result")
    body = source if source is not None else payload
    status = pick_str(body, "status")
    sub_status = pick_str(body, "subStatus")
    if status in {"PENDING", "PROCESSING"}:
        return ReportPoll(kind="wait", file_url=None, yandex_report_id=report_id)
    if sub_status == "NO_DATA":
        return ReportPoll(kind="no_data", file_url=None, yandex_report_id=report_id)
    if sub_status == "TOO_LARGE":
        return ReportPoll(kind="too_large", file_url=None, yandex_report_id=report_id)
    if status == "DONE":
        return ReportPoll(
            kind="done",
            file_url=pick_str(body, "file"),
            yandex_report_id=report_id,
        )
    if status == "FAILED":
        kind = "not_found" if sub_status == "RESOURCE_NOT_FOUND" else "failed"
        return ReportPoll(kind=kind, file_url=None, yandex_report_id=report_id)
    return ReportPoll(kind="unknown", file_url=None, yandex_report_id=report_id)


async def poll_report(
    client: YandexMarketClient,
    report_id: str,
    *,
    timeout_seconds: float,
    pause_seconds: float,
    sleep: Sleep,
) -> ReportPoll:
    if _REPORT_ID.fullmatch(report_id) is None:
        raise ReportDownloadError("report id was missing")
    deadline = time.monotonic() + timeout_seconds
    while True:
        payload = await client.get(f"/v2/reports/info/{report_id}", {})
        state = interpret_report_info(payload, report_id)
        if state.kind != "wait":
            return state
        if time.monotonic() >= deadline:
            raise ReportTimeout("report was not ready before the timeout")
        await sleep(pause_seconds)


async def download_report_file(
    url: str,
    *,
    max_bytes: int = MAX_REPORT_BYTES,
    timeout: float = 60,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    current = url
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        headers={},
    ) as client:
        for _ in range(4):
            _require_https(current)
            try:
                response = await client.get(current)
            except httpx.TransportError as exc:
                raise ReportDownloadError("report file was not downloaded") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if location is None:
                    raise ReportDownloadError("report file was not downloaded")
                current = location
                continue
            if response.status_code != 200:
                raise ReportDownloadError("report file was not downloaded")
            return _limited_body(response.content, max_bytes)
    raise ReportDownloadError("report file was not downloaded")


def parse_report_bytes(content: bytes) -> list[tuple[str, JsonObject]]:
    if content.startswith(b"PK"):
        return _parse_zip(content)
    return _rows_from_document(content, "report")


def _parse_zip(content: bytes) -> list[tuple[str, JsonObject]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ReportParseError("report file could not be read") from exc
    names = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".json") and not name.endswith("/")
    ]
    if len(names) > _MAX_ARCHIVE_FILES:
        raise ReportParseError("report file could not be read")
    rows: list[tuple[str, JsonObject]] = []
    total = 0
    for name in names:
        info = archive.getinfo(name)
        total += info.file_size
        if total > _MAX_UNCOMPRESSED_BYTES:
            raise ReportParseError("report file could not be read")
        sheet = name.rsplit("/", 1)[-1].removesuffix(".json") or "report"
        rows.extend(_rows_from_document(archive.read(name), sheet))
    return rows


def _rows_from_document(content: bytes, sheet: str) -> list[tuple[str, JsonObject]]:
    try:
        parsed = expect_json(loads(content))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ReportParseError("report file could not be read") from exc
    collected: list[tuple[str, JsonObject]] = []
    _collect(parsed, sheet, collected)
    return collected


def _collect(value: JsonValue, sheet: str, collected: list[tuple[str, JsonObject]]) -> None:
    if isinstance(value, dict):
        rows = value.get("rows")
        if isinstance(rows, list):
            for item in as_object_list(rows):
                collected.append((sheet, item))
            return
        nested = False
        for key, item in value.items():
            if isinstance(item, list) and as_object_list(item):
                nested = True
                for row in as_object_list(item):
                    collected.append((f"{sheet}:{key}", row))
        if nested:
            return
        collected.append((sheet, value))
        return
    if isinstance(value, list):
        for item in as_object_list(value):
            collected.append((sheet, item))


def _limited_body(content: bytes, max_bytes: int) -> bytes:
    if len(content) > max_bytes:
        raise ReportDownloadError("report file was not downloaded")
    return content


def _require_https(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or parsed.username or parsed.password or host is None:
        raise ReportDownloadError("report url was rejected")
    lowered = host.lower()
    if lowered in {"localhost", "metadata.google.internal"} or lowered.endswith(".local"):
        raise ReportDownloadError("report url was rejected")
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ReportDownloadError("report url was rejected")


def _last_day(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    if month in {4, 6, 9, 11}:
        return 30
    return 31
