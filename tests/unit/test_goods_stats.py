import json

import httpx
from pytest import CaptureFixture

from profitsamurai.integrations.yandex_market.goods_stats import parse_offer_ids, print_goods_stats


def test_parse_offer_ids_reads_result_and_next_token() -> None:
    offer_ids, token = parse_offer_ids(
        {
            "result": {
                "paging": {"nextPageToken": "next"},
                "offers": [{"offerId": "sku-1"}, {"status": "PUBLISHED"}, {"offerId": ""}],
            }
        }
    )
    assert offer_ids == ["sku-1"]
    assert token == "next"


async def test_print_goods_stats_requests_skus_and_prints_the_report(
    capsys: CaptureFixture[str],
) -> None:
    calls: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.content))
        if request.url.path.endswith("/offers"):
            token = request.url.params.get("pageToken")
            if token is None:
                body = {
                    "result": {
                        "paging": {"nextPageToken": "p2"},
                        "offers": [{"offerId": "sku-1"}],
                    }
                }
            else:
                body = {"result": {"offers": [{"offerId": "sku-2"}, {"offerId": "sku-1"}]}}
            return httpx.Response(200, json=body)
        report = {"status": "OK", "result": {"shopSkus": [{"shopSku": "sku-1"}]}}
        return httpx.Response(200, json=report)

    await print_goods_stats(
        "secret",
        "https://api.partner.market.yandex.ru",
        10,
        transport=httpx.MockTransport(handler),
        attempts=1,
        sku_batch=1,
    )
    assert [path for path, _body in calls] == [
        "/v2/campaigns/10/offers",
        "/v2/campaigns/10/offers",
        "/v2/campaigns/10/stats/skus",
        "/v2/campaigns/10/stats/skus",
    ]
    assert json.loads(calls[2][1]) == {"shopSkus": ["sku-1"]}
    assert json.loads(calls[3][1]) == {"shopSkus": ["sku-2"]}
    captured = capsys.readouterr()
    assert '"shopSku": "sku-1"' in captured.out
