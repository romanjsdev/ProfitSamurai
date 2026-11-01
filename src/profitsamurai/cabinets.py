"""Write the identifiers returned by GET /v2/campaigns."""

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from profitsamurai.db.service.models import Cabinet, Campaign
from profitsamurai.integrations.yandex_market.campaigns import RemoteCampaign
from profitsamurai.security import encrypt_secret
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


async def save_cabinets(
    session: AsyncSession,
    *,
    fernet_key: str,
    api_key: str,
    campaigns: list[RemoteCampaign],
) -> list[StoredCabinet]:
    stored = group_campaigns(campaigns)
    if not stored:
        return []
    ciphertext = encrypt_secret(api_key, fernet_key)
    for cabinet in stored:
        await session.execute(
            insert(Cabinet)
            .values(business_id=cabinet.business_id, api_key_ciphertext=ciphertext)
            .on_conflict_do_update(
                index_elements=[Cabinet.business_id],
                set_={"api_key_ciphertext": ciphertext},
            )
        )
        await session.execute(
            insert(Campaign)
            .values(
                [
                    {"campaign_id": campaign_id, "business_id": cabinet.business_id}
                    for campaign_id in cabinet.campaign_ids
                ]
            )
            .on_conflict_do_update(
                index_elements=[Campaign.campaign_id],
                set_={"business_id": cabinet.business_id},
            )
        )
        await session.execute(
            delete(Campaign).where(
                Campaign.business_id == cabinet.business_id,
                Campaign.campaign_id.not_in(cabinet.campaign_ids),
            )
        )
    await session.commit()
    return stored


async def load_cabinet(session: AsyncSession, business_id: int) -> StoredCabinet | None:
    cabinet = await session.get(Cabinet, business_id)
    if cabinet is None:
        return None
    rows = await session.scalars(
        select(Campaign.campaign_id)
        .where(Campaign.business_id == business_id)
        .order_by(Campaign.campaign_id)
    )
    campaign_ids = tuple(CampaignId(campaign_id) for campaign_id in rows)
    return StoredCabinet(business_id=BusinessId(cabinet.business_id), campaign_ids=campaign_ids)
