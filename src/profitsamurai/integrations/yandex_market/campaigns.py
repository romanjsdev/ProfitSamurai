"""GET /v2/campaigns — the first Partner API call."""

from dataclasses import dataclass

import httpx

from profitsamurai.integrations.yandex_market.client import YandexMarketClient
from profitsamurai.jsonutil import JsonObject, as_object_list, nested_object, pick_int
from profitsamurai.types import BusinessId, CampaignId


@dataclass(frozen=True)
class RemoteCampaign:
    business_id: BusinessId
    campaign_id: CampaignId


def parse_campaign_page(payload: JsonObject) -> tuple[list[RemoteCampaign], int | None]:
    source = nested_object(payload, "result")
    body = source if source is not None else payload
    found: list[RemoteCampaign] = []
    for row in as_object_list(body.get("campaigns")):
        campaign_id = pick_int(row, "id")
        business = nested_object(row, "business")
        business_id = pick_int(business, "id") if business is not None else None
        if campaign_id is None or business_id is None:
            continue
        found.append(
            RemoteCampaign(
                business_id=BusinessId(business_id),
                campaign_id=CampaignId(campaign_id),
            )
        )
    pager = nested_object(body, "pager")
    pages = pick_int(pager, "pagesCount") if pager is not None else None
    return found, pages


async def fetch_campaigns(
    api_key: str,
    base_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    attempts: int = 5,
    wait_seconds: float = 1.0,
) -> list[RemoteCampaign]:
    found: list[RemoteCampaign] = []
    seen: set[int] = set()
    async with YandexMarketClient(
        api_key=api_key,
        base_url=base_url,
        transport=transport,
        attempts=attempts,
        wait_seconds=wait_seconds,
    ) as client:
        for page in range(1, 1_001):
            payload = await client.get(
                "/v2/campaigns",
                {"page": str(page), "pageSize": "100"},
            )
            batch, pages = parse_campaign_page(payload)
            for row in batch:
                if row.campaign_id in seen:
                    continue
                seen.add(row.campaign_id)
                found.append(row)
            if pages is None or page >= pages:
                return found
    return found
