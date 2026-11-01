from profitsamurai.integrations.yandex_market.campaigns import parse_campaign_page
from profitsamurai.jsonutil import expect_object


def test_parse_keeps_only_ids() -> None:
    payload = expect_object(
        {
            "campaigns": [
                {
                    "id": 10,
                    "domain": "shop.example",
                    "placementType": "FBS",
                    "express": True,
                    "business": {"id": 5, "name": "Acme"},
                }
            ],
            "pager": {"pagesCount": 1, "currentPage": 1},
        }
    )
    rows, pages = parse_campaign_page(payload)
    assert pages == 1
    assert len(rows) == 1
    assert rows[0].business_id == 5
    assert rows[0].campaign_id == 10


def test_parse_skips_a_campaign_without_a_business() -> None:
    payload = expect_object({"campaigns": [{"id": 10, "domain": "shop"}]})
    rows, pages = parse_campaign_page(payload)
    assert rows == []
    assert pages is None


def test_parse_reads_result_wrapper() -> None:
    payload = expect_object(
        {"result": {"campaigns": [{"id": 3, "business": {"id": 8}}], "pager": {"pagesCount": 2}}}
    )
    rows, pages = parse_campaign_page(payload)
    assert pages == 2
    assert [(row.business_id, row.campaign_id) for row in rows] == [(8, 3)]
