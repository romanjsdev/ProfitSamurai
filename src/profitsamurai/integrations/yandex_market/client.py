"""HTTP client for https://api.partner.market.yandex.ru."""

import asyncio
import json
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
    ) -> JsonObject:
        return await self._send("POST", path, params=params, body=body)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        body: JsonObject | None,
    ) -> JsonObject:
        http = self._require_http()
        for attempt in range(1, self._attempts + 1):
            try:
                response = await http.request(method, path, params=params, json=body)
            except httpx.TransportError:
                if attempt == self._attempts:
                    raise
                await asyncio.sleep(self._wait_seconds)
                continue
            if response.status_code in {420, 429}:
                if attempt == self._attempts:
                    raise RateLimitError("rate limited", status_code=response.status_code)
                await asyncio.sleep(self._wait_seconds)
                continue
            if response.status_code >= 500:
                if attempt == self._attempts:
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
