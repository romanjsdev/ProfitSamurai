"""Register a seller company and attach a Yandex Market shop."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from profitsamurai.api.deps import get_service_session, require_bootstrap
from profitsamurai.api.schemas.cabinets import CabinetCreate, CabinetOut
from profitsamurai.api.schemas.clients import ClientCreate, ClientOut
from profitsamurai.config import Settings, get_settings
from profitsamurai.integrations.yandex_market.campaigns import fetch_campaigns
from profitsamurai.integrations.yandex_market.errors import YandexMarketError
from profitsamurai.registry import (
    ClientNotReady,
    ProvisionError,
    attach_shops,
    load_client,
    provision_client,
    provisioning_configured,
    register_client,
)

router = APIRouter(tags=["clients"])


def _out(record_id: UUID, name: str, database_name: str, status: str) -> ClientOut:
    return ClientOut(id=record_id, name=name, database_name=database_name, status=status)


def _cabinet(business_id: int, campaign_ids: tuple[int, ...]) -> CabinetOut:
    return CabinetOut(business_id=business_id, campaign_ids=list(campaign_ids))


@router.post("/clients", response_model=ClientOut)
async def create_client(
    body: ClientCreate,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
    settings: Settings = Depends(get_settings),
) -> ClientOut:
    if not provisioning_configured(settings):
        raise HTTPException(
            status_code=503,
            detail="client database provisioning is not configured",
        )
    record = await register_client(session, body.name)
    return await _finish_provision(session, settings, record.id)


@router.post("/clients/{client_id}/provision", response_model=ClientOut)
async def retry_provision(
    client_id: UUID,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
    settings: Settings = Depends(get_settings),
) -> ClientOut:
    if not provisioning_configured(settings):
        raise HTTPException(
            status_code=503,
            detail="client database provisioning is not configured",
        )
    return await _finish_provision(session, settings, client_id)


@router.get("/clients/{client_id}", response_model=ClientOut)
async def read_client(
    client_id: UUID,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
) -> ClientOut:
    record = await load_client(session, client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="client not found")
    return _out(record.id, record.name, record.database_name, record.status)


@router.post("/clients/{client_id}/shops", response_model=list[CabinetOut])
async def connect_shop(
    client_id: UUID,
    body: CabinetCreate,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
    settings: Settings = Depends(get_settings),
) -> list[CabinetOut]:
    try:
        remote = await fetch_campaigns(body.api_key, settings.ym_api_base)
    except YandexMarketError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        stored = await attach_shops(session, settings, client_id, body.api_key, remote)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="client not found") from exc
    except ClientNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProvisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not stored:
        raise HTTPException(status_code=422, detail="campaigns did not include a business id")
    return [_cabinet(item.business_id, item.campaign_ids) for item in stored]


async def _finish_provision(
    session: AsyncSession,
    settings: Settings,
    client_id: UUID,
) -> ClientOut:
    try:
        record = await provision_client(session, settings, client_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="client not found") from exc
    except ProvisionError as exc:
        current = await load_client(session, client_id)
        detail: dict[str, str] = {
            "id": str(client_id),
            "status": current.status if current is not None else "failed",
            "detail": str(exc),
        }
        if current is not None:
            detail["database_name"] = current.database_name
        raise HTTPException(status_code=502, detail=detail) from exc
    return _out(record.id, record.name, record.database_name, record.status)
