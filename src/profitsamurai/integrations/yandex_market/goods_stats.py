"""POST /v2/campaigns/{campaignId}/stats/skus. The body is printed and not stored."""

import json

import httpx

from profitsamurai.integrations.yandex_market.client import YandexMarketClient
from profitsamurai.jsonutil import JsonObject, JsonValue, as_object_list, nested_object, pick_str

_OFFER_PAGE = 200
_SKU_BATCH = 500


def parse_offer_ids(payload: JsonObject) -> tuple[list[str], str | None]:
    source = nested_object(payload, "result")
    body = source if source is not None else payload
    found: list[str] = []
    for row in as_object_list(body.get("offers")):
        offer_id = pick_str(row, "offerId")
        if offer_id is not None:
            found.append(offer_id)
    paging = nested_object(body, "paging")
    token = pick_str(paging, "nextPageToken") if paging is not None else None
    return found, token


async def print_goods_stats(
    api_key: str,
    base_url: str,
    campaign_id: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    attempts: int = 5,
    wait_seconds: float = 1.0,
    sku_batch: int = _SKU_BATCH,
) -> None:
    async with YandexMarketClient(
        api_key=api_key,
        base_url=base_url,
        transport=transport,
        attempts=attempts,
        wait_seconds=wait_seconds,
    ) as client:
        offer_ids = await _offer_ids(client, campaign_id)
        if not offer_ids:
            print(f"campaign {campaign_id} stats/skus: no offers")
            return
        for start in range(0, len(offer_ids), sku_batch):
            body: JsonObject = {"shopSkus": _json_list(offer_ids[start : start + sku_batch])}
            report = await client.post(f"/v2/campaigns/{campaign_id}/stats/skus", body)
            print(json.dumps(report, ensure_ascii=False))


async def _offer_ids(client: YandexMarketClient, campaign_id: int) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    token: str | None = None
    for _page in range(1_000):
        params = {"limit": str(_OFFER_PAGE)}
        if token is not None:
            params["pageToken"] = token
        payload = await client.post(f"/v2/campaigns/{campaign_id}/offers", {}, params)
        batch, token = parse_offer_ids(payload)
        for offer_id in batch:
            if offer_id in seen:
                continue
            seen.add(offer_id)
            found.append(offer_id)
        if token is None:
            return found
    return found


def _json_list(items: list[str]) -> list[JsonValue]:
    return list(items)
