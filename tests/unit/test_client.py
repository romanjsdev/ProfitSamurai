import httpx

from profitsamurai.integrations.yandex_market.campaigns import fetch_campaigns
from profitsamurai.integrations.yandex_market.errors import YandexMarketError


def _page(campaigns: list[object], pages: int) -> dict[str, object]:
    return {"campaigns": campaigns, "pager": {"pagesCount": pages}}


async def test_fetch_walks_pages_and_sends_the_api_key() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Api-Key"])
        assert request.url.path == "/v2/campaigns"
        page = request.url.params["page"]
        if page == "1":
            body = _page([{"id": 10, "business": {"id": 5}, "placementType": "FBS"}], 2)
        else:
            body = _page([{"id": 11, "business": {"id": 5, "name": "Acme"}}], 2)
        return httpx.Response(200, json=body)

    rows = await fetch_campaigns(
        "secret",
        "https://api.partner.market.yandex.ru",
        transport=httpx.MockTransport(handler),
        attempts=1,
    )
    assert seen == ["secret", "secret"]
    assert [(row.business_id, row.campaign_id) for row in rows] == [(5, 10), (5, 11)]


async def test_fetch_does_not_retry_a_client_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"status": "ERROR"})

    try:
        await fetch_campaigns(
            "secret",
            "https://api.partner.market.yandex.ru",
            transport=httpx.MockTransport(handler),
            attempts=3,
            wait_seconds=0,
        )
    except YandexMarketError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected YandexMarketError")
    assert calls == 1
