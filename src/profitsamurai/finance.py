"""Import Yandex Market financial reports into a client's raw schema."""

import hashlib
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.integrations.yandex_market.client import YandexMarketClient
from profitsamurai.integrations.yandex_market.errors import (
    RateLimitError,
    TransientStatusError,
    YandexMarketError,
)
from profitsamurai.integrations.yandex_market.reports import (
    ReportDownloadError,
    ReportParseError,
    ReportRequest,
    ReportTimeout,
    ShopCampaign,
    Sleep,
    build_report_requests,
    download_report_file,
    generation_interval_seconds,
    parse_report_bytes,
    poll_report,
    report_id_from,
)
from profitsamurai.jsonutil import JsonObject

logger = logging.getLogger(__name__)

_FAILURES = {
    "too_large": "report is too large",
    "failed": "report generation failed",
    "not_found": "report source was not found",
    "unknown": "report status was not recognized",
}
_UNKNOWN_PREVIOUS = "previous report generation result is unknown"


class FinanceImportBusy(Exception):
    """Another import of this shop is still running."""


class ReportStore(Protocol):
    async def next_version(self, shop_id: UUID, period_from: date, period_to: date) -> int: ...

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
    ) -> UUID: ...

    async def save_file(self, import_id: UUID, content: bytes) -> None: ...

    async def save_rows(self, import_id: UUID, rows: list[tuple[str, JsonObject]]) -> None: ...

    async def finish(
        self,
        import_id: UUID,
        *,
        status: str,
        row_count: int,
        error_message: str | None,
        yandex_report_id: str | None,
    ) -> None: ...


@dataclass(frozen=True)
class ReportOutcome:
    report_type: str
    campaign_id: int | None
    status: str
    version: int
    row_count: int
    error_message: str | None
    yandex_report_id: str | None
    blocks_next: bool = False


@dataclass(frozen=True)
class FinanceImportResult:
    version: int
    reports: list[ReportOutcome]


def advisory_keys(shop_id: UUID) -> tuple[int, int]:
    """One lock per shop, so two periods cannot generate reports at the same time."""
    digest = hashlib.sha256(f"finance-import:{shop_id}".encode()).digest()
    first = int.from_bytes(digest[:4], "big", signed=True)
    second = int.from_bytes(digest[4:8], "big", signed=True)
    return first, second


def public_error(exc: BaseException) -> str:
    if isinstance(exc, ReportTimeout | ReportDownloadError | ReportParseError):
        message = str(exc).strip()
        return message or "finance import failed"
    if isinstance(exc, httpx.TransportError):
        return "partner api connection failed"
    if isinstance(exc, YandexMarketError):
        if exc.status_code is None:
            return "partner api error"
        return f"partner api error {exc.status_code}"
    return "finance import failed"


@asynccontextmanager
async def hold_import_lock(
    conn: AsyncConnection,
    first: int,
    second: int,
) -> AsyncGenerator[None, None]:
    locked = await conn.scalar(
        text("SELECT pg_try_advisory_lock(:first, :second)"),
        {"first": first, "second": second},
    )
    if not locked:
        raise FinanceImportBusy("finance import is already running")
    try:
        yield
    finally:
        await conn.execute(
            text("SELECT pg_advisory_unlock(:first, :second)"),
            {"first": first, "second": second},
        )


@asynccontextmanager
async def tenant_import_lock(
    database_url: str,
    shop_id: UUID,
) -> AsyncGenerator[None, None]:
    first, second = advisory_keys(shop_id)
    engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            async with hold_import_lock(conn, first, second):
                yield
    finally:
        await engine.dispose()


async def import_reports(
    *,
    api_key: str,
    base_url: str,
    business_id: int,
    shop_id: UUID,
    campaigns: list[ShopCampaign],
    period_from: date,
    period_to: date,
    store: ReportStore,
    transport: httpx.AsyncBaseTransport | None = None,
    download_transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = 300,
    pause_seconds: float = 10,
    gap_seconds: float | None = None,
    sleep: Sleep | None = None,
    attempts: int = 3,
) -> FinanceImportResult:
    """Generate the three financial reports one after another and store each result."""
    requests = build_report_requests(
        business_id=business_id,
        campaigns=campaigns,
        period_from=period_from,
        period_to=period_to,
    )
    pause = _sleep if sleep is None else sleep
    version = await store.next_version(shop_id, period_from, period_to)
    outcomes: list[ReportOutcome] = []
    last_generate: dict[str, float] = {}
    blocked = False
    async with YandexMarketClient(
        api_key=api_key,
        base_url=base_url,
        transport=transport,
        attempts=attempts,
        wait_seconds=0 if transport is not None else 1,
    ) as client:
        for request in requests:
            if blocked:
                outcomes.append(
                    await _not_requested(
                        request,
                        store=store,
                        shop_id=shop_id,
                        business_id=business_id,
                        period_from=period_from,
                        period_to=period_to,
                        version=version,
                    )
                )
                continue
            await _wait_for_generate_slot(request.path, last_generate, gap_seconds, pause)
            outcome = await _import_one(
                client,
                request,
                store=store,
                shop_id=shop_id,
                business_id=business_id,
                period_from=period_from,
                period_to=period_to,
                version=version,
                download_transport=download_transport,
                timeout_seconds=timeout_seconds,
                pause_seconds=pause_seconds,
                sleep=pause,
                attempts=attempts,
            )
            outcomes.append(outcome)
            last_generate[request.path] = time.monotonic()
            blocked = outcome.blocks_next
    return FinanceImportResult(version=version, reports=outcomes)


async def _import_one(
    client: YandexMarketClient,
    request: ReportRequest,
    *,
    store: ReportStore,
    shop_id: UUID,
    business_id: int,
    period_from: date,
    period_to: date,
    version: int,
    download_transport: httpx.AsyncBaseTransport | None,
    timeout_seconds: float,
    pause_seconds: float,
    sleep: Sleep,
    attempts: int,
) -> ReportOutcome:
    import_id = await store.begin(
        shop_id=shop_id,
        business_id=business_id,
        report_type=request.report_type,
        period_from=period_from,
        period_to=period_to,
        campaign_id=request.campaign_id,
        version=version,
    )
    yandex_report_id: str | None = None
    stage = "generate"
    saved_rows: int | None = None
    try:
        generated = await _generate(client, request, sleep=sleep, attempts=attempts)
        yandex_report_id = report_id_from(generated)
        state = await poll_report(
            client,
            yandex_report_id,
            timeout_seconds=timeout_seconds,
            pause_seconds=pause_seconds,
            sleep=sleep,
        )
        yandex_report_id = state.yandex_report_id
        if state.kind == "no_data":
            await _finish(store, import_id, "no_data", 0, None, yandex_report_id)
            return _outcome(request, version, "no_data", 0, None, yandex_report_id, False)
        if state.kind != "done":
            message = _FAILURES.get(state.kind, _FAILURES["unknown"])
            status = _stored_status(state.kind)
            await _finish(store, import_id, status, 0, message, yandex_report_id)
            return _outcome(request, version, status, 0, message, yandex_report_id, False)
        if state.file_url is None:
            message = "report file link is missing"
            await _finish(store, import_id, "failed", 0, message, yandex_report_id)
            return _outcome(request, version, "failed", 0, message, yandex_report_id, False)
        stage = "download"
        content = await download_report_file(state.file_url, transport=download_transport)
        stage = "file"
        await store.save_file(import_id, content)
        stage = "parse"
        rows = parse_report_bytes(content)
        stage = "rows"
        await store.save_rows(import_id, rows)
        saved_rows = len(rows)
        stage = "finalize"
        await _finish(store, import_id, "done", saved_rows, None, yandex_report_id)
        return _outcome(request, version, "done", saved_rows, None, yandex_report_id, False)
    except Exception as exc:
        message = _stage_message(stage, exc)
        blocks_next = _blocks_next(exc) if stage == "generate" else False
        row_count = 0 if saved_rows is None else saved_rows
        recorded = await _record_failure(
            store,
            import_id,
            row_count=row_count,
            error_message=message,
            yandex_report_id=yandex_report_id,
        )
        status = "failed" if recorded else "running"
        return _outcome(
            request,
            version,
            status,
            row_count,
            message,
            yandex_report_id,
            blocks_next,
        )


def _stage_message(stage: str, exc: BaseException) -> str:
    """Name the failed step without copying an unsafe exception text."""
    if stage == "download":
        if isinstance(exc, ReportDownloadError):
            detail = str(exc).strip()
            if detail:
                return f"report download failed: {detail}"
        return "report download failed"
    if stage == "parse":
        if isinstance(exc, ReportParseError):
            detail = str(exc).strip()
            if detail:
                return detail
        return "report parse failed"
    if stage == "file":
        return "report file save failed"
    if stage == "rows":
        return "report row save failed"
    if stage == "finalize":
        return "report finalize failed"
    return public_error(exc)


async def _record_failure(
    store: ReportStore,
    import_id: UUID,
    *,
    row_count: int,
    error_message: str,
    yandex_report_id: str | None,
) -> bool:
    """Record a failed attempt. A second failure must not zero a saved row count."""
    try:
        await store.finish(
            import_id,
            status="failed",
            row_count=row_count,
            error_message=error_message,
            yandex_report_id=yandex_report_id,
        )
    except Exception:
        logger.info("finance import %s failure was not recorded", import_id)
        return False
    return True


def _stored_status(kind: str) -> str:
    if kind == "too_large":
        return "too_large"
    return "failed"


def _outcome(
    request: ReportRequest,
    version: int,
    status: str,
    row_count: int,
    error_message: str | None,
    yandex_report_id: str | None,
    blocks_next: bool,
) -> ReportOutcome:
    logger.info("finance report %s finished with %s", request.report_type, status)
    return ReportOutcome(
        report_type=request.report_type,
        campaign_id=request.campaign_id,
        status=status,
        version=version,
        row_count=row_count,
        error_message=error_message,
        yandex_report_id=yandex_report_id,
        blocks_next=blocks_next,
    )


def _blocks_next(exc: BaseException) -> bool:
    """A lost or unfinished generate call may still be running at Market."""
    if isinstance(exc, httpx.TransportError | TransientStatusError | ReportTimeout):
        return True
    return isinstance(exc, YandexMarketError) and exc.status_code == 499


async def _not_requested(
    request: ReportRequest,
    *,
    store: ReportStore,
    shop_id: UUID,
    business_id: int,
    period_from: date,
    period_to: date,
    version: int,
) -> ReportOutcome:
    import_id = await store.begin(
        shop_id=shop_id,
        business_id=business_id,
        report_type=request.report_type,
        period_from=period_from,
        period_to=period_to,
        campaign_id=request.campaign_id,
        version=version,
    )
    await _finish(store, import_id, "failed", 0, _UNKNOWN_PREVIOUS, None)
    return _outcome(request, version, "failed", 0, _UNKNOWN_PREVIOUS, None, True)


async def _finish(
    store: ReportStore,
    import_id: UUID,
    status: str,
    row_count: int,
    error_message: str | None,
    yandex_report_id: str | None,
) -> None:
    await store.finish(
        import_id,
        status=status,
        row_count=row_count,
        error_message=error_message,
        yandex_report_id=yandex_report_id,
    )


async def _wait_for_generate_slot(
    path: str,
    last_generate: dict[str, float],
    gap_seconds: float | None,
    sleep: Sleep,
) -> None:
    previous = last_generate.get(path)
    if previous is None:
        return
    interval = gap_seconds if gap_seconds is not None else generation_interval_seconds(path)
    remaining = interval - (time.monotonic() - previous)
    if remaining > 0:
        await sleep(remaining)


async def _generate(
    client: YandexMarketClient,
    request: ReportRequest,
    *,
    sleep: Sleep,
    attempts: int,
) -> JsonObject:
    """POST a generate method once, and repeat it only when Market rejected the call."""
    last_limit: RateLimitError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await client.post(
                request.path,
                request.body,
                request.params,
                idempotent=False,
            )
        except RateLimitError as exc:
            last_limit = exc
            if attempt == attempts:
                raise
            await sleep(exc.wait_seconds)
    if last_limit is not None:
        raise last_limit
    raise YandexMarketError("request was not completed")


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
