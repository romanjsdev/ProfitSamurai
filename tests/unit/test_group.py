from profitsamurai.cabinets import group_campaigns
from profitsamurai.integrations.yandex_market.campaigns import RemoteCampaign
from profitsamurai.types import BusinessId, CampaignId


def test_group_sorts_and_keeps_one_owner() -> None:
    rows = [
        RemoteCampaign(BusinessId(2), CampaignId(30)),
        RemoteCampaign(BusinessId(1), CampaignId(10)),
        RemoteCampaign(BusinessId(1), CampaignId(11)),
        RemoteCampaign(BusinessId(2), CampaignId(10)),
    ]
    stored = group_campaigns(rows)
    assert [(item.business_id, item.campaign_ids) for item in stored] == [
        (1, (11,)),
        (2, (10, 30)),
    ]
