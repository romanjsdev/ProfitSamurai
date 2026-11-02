"""Group Yandex campaigns by the business that owns them."""

from dataclasses import dataclass

from profitsamurai.integrations.yandex_market.campaigns import RemoteCampaign
from profitsamurai.types import BusinessId, CampaignId


@dataclass(frozen=True)
class StoredCabinet:
    business_id: BusinessId
    campaign_ids: tuple[CampaignId, ...]


def group_campaigns(rows: list[RemoteCampaign]) -> list[StoredCabinet]:
    """One campaign id belongs to one business. The later row wins."""
    owner: dict[CampaignId, BusinessId] = {}
    for row in rows:
        owner[row.campaign_id] = row.business_id
    grouped: dict[BusinessId, list[CampaignId]] = {}
    for campaign_id, business_id in owner.items():
        grouped.setdefault(business_id, []).append(campaign_id)
    stored: list[StoredCabinet] = []
    for business_id in sorted(grouped):
        campaign_ids = tuple(sorted(grouped[business_id]))
        stored.append(StoredCabinet(business_id=business_id, campaign_ids=campaign_ids))
    return stored
