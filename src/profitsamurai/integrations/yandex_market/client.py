"""HTTP client for https://api.partner.market.yandex.ru."""

import asyncio
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Self

import httpx

from profitsamurai.integrations.yandex_market.errors import (
    RateLimitError,
    TransientStatusError,
    YandexMarketError,
)
from profitsamurai.jsonutil import JsonObject, expect_object, loads


class YandexMarketClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        attempts: int = 5,
        wait_seconds: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._attempts = attempts
        self._wait_seconds = wait_seconds
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Api-Key": self._api_key},
            timeout=httpx.Timeout(30.0),
            transport=self._transport,
            follow_redirects=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get(self, path: str, params: dict[str, str]) -> JsonObject:
        return await self._send("GET", path, params=params, body=None)

    async def post(
        self,
        path: str,
        body: JsonObject,
        params: dict[str, str] | None = None,
        *,
        idempotent: bool = True,
    ) -> JsonObject:
        return await self._send("POST", path, params=params, body=body, idempotent=idempotent)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        body: JsonObject | None,
        idempotent: bool = True,
    ) -> JsonObject:
        http = self._require_http()
        for attempt in range(1, self._attempts + 1):
            try:
                response = await http.request(method, path, params=params, json=body)
            except httpx.TransportError:
                if not idempotent or attempt == self._attempts:
                    raise
                await asyncio.sleep(self._wait_seconds)
                continue
            if response.status_code in {420, 429}:
                delay = rate_limit_wait_seconds(
                    response.headers,
                    fallback=self._wait_seconds if idempotent else 120,
                )
                if idempotent and attempt < self._attempts:
                    await asyncio.sleep(delay)
                    continue
                raise RateLimitError(
                    "rate limited",
                    status_code=response.status_code,
                    wait_seconds=delay,
                )
            if response.status_code >= 500:
                if not idempotent or attempt == self._attempts:
                    raise TransientStatusError(
                        f"status {response.status_code}",
                        status_code=response.status_code,
                    )
                await asyncio.sleep(self._wait_seconds)
                continue
            return _parse_response(response)
        raise YandexMarketError("request was not completed")

    def _require_http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Yandex Market client is not open")
        return self._http


def rate_limit_wait_seconds(
    headers: httpx.Headers,
    *,
    fallback: float,
    now: datetime | None = None,
) -> float:
    """Prefer Retry-After, then the Market resource window, otherwise the fallback delay."""
    moment = now or datetime.now(UTC)
    waits: list[float] = []
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        parsed = _delay_until(retry_after, moment)
        if parsed is not None:
            waits.append(parsed)
    until = headers.get("X-RateLimit-Resource-Until")
    if until is not None:
        parsed = _delay_until(until, moment)
        if parsed is not None:
            waits.append(parsed)
    if not waits:
        return fallback
    return min(max(waits), 600)


def _delay_until(value: str, now: datetime) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if seconds > 0 else None
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return (target - now).total_seconds()


def _parse_response(response: httpx.Response) -> JsonObject:
    try:
        payload = loads(response.content)
    except json.JSONDecodeError as exc:
        raise YandexMarketError("response was not JSON", status_code=response.status_code) from exc
    if response.status_code >= 400:
        raise YandexMarketError(
            "partner api rejected the request",
            status_code=response.status_code,
        )
    body = expect_object(payload)
    if body.get("status") == "ERROR":
        raise YandexMarketError("partner api error", status_code=response.status_code)
    return body
