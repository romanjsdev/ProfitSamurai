"""Sync cabinets from GET /v2/campaigns and read them back."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from profitsamurai.api.deps import get_service_session, require_bootstrap
from profitsamurai.api.schemas.cabinets import CabinetCreate, CabinetOut
from profitsamurai.cabinets import load_cabinet, save_cabinets
from profitsamurai.config import Settings, get_settings
from profitsamurai.integrations.yandex_market.campaigns import fetch_campaigns
from profitsamurai.integrations.yandex_market.errors import YandexMarketError

router = APIRouter(tags=["cabinets"])


def _out(business_id: int, campaign_ids: tuple[int, ...]) -> CabinetOut:
    return CabinetOut(business_id=business_id, campaign_ids=list(campaign_ids))


@router.post("/cabinets", response_model=list[CabinetOut])
async def sync_cabinets(
    body: CabinetCreate,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
    settings: Settings = Depends(get_settings),
) -> list[CabinetOut]:
    try:
        remote = await fetch_campaigns(body.api_key, settings.ym_api_base)
    except YandexMarketError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    stored = await save_cabinets(
        session,
        fernet_key=settings.fernet_key,
        api_key=body.api_key,
        campaigns=remote,
    )
    if not stored:
        raise HTTPException(status_code=422, detail="campaigns did not include a business id")
    return [_out(item.business_id, item.campaign_ids) for item in stored]


@router.get("/cabinets/{business_id}", response_model=CabinetOut)
async def read_cabinet(
    business_id: int,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
) -> CabinetOut:
    stored = await load_cabinet(session, business_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="cabinet not found")
    return _out(stored.business_id, stored.campaign_ids)
