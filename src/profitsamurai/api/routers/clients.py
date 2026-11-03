"""Register a seller company and attach a Yandex Market shop."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from profitsamurai.api.deps import get_service_session, require_bootstrap
from profitsamurai.api.schemas.clients import ClientCreate, ClientOut
from profitsamurai.api.schemas.finance import (
    FinanceImportCreate,
    FinanceImportOut,
    FinanceReportOut,
)
from profitsamurai.api.schemas.shops import ShopCreate, ShopOut
from profitsamurai.config import Settings, get_settings
from profitsamurai.db.tenant.finance_store import SqlReportStore
from profitsamurai.finance import FinanceImportBusy, import_reports, tenant_import_lock
from profitsamurai.integrations.yandex_market.campaigns import fetch_campaigns
from profitsamurai.integrations.yandex_market.errors import YandexMarketError
from profitsamurai.integrations.yandex_market.reports import ShopCampaign
from profitsamurai.registry import (
    ClientNotReady,
    ProvisionError,
    attach_shops,
    load_client,
    load_shop,
    provision_client,
    provisioning_configured,
    read_tenant_campaigns,
    register_client,
    tenant_database_url,
)
from profitsamurai.security import decrypt_secret

router = APIRouter(tags=["clients"])


def _out(record_id: UUID, name: str, database_name: str, status: str) -> ClientOut:
    return ClientOut(id=record_id, name=name, database_name=database_name, status=status)


def _shop(business_id: int, campaign_ids: tuple[int, ...]) -> ShopOut:
    return ShopOut(business_id=business_id, campaign_ids=list(campaign_ids))


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


@router.post("/clients/{client_id}/shops", response_model=list[ShopOut])
async def connect_shop(
    client_id: UUID,
    body: ShopCreate,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
    settings: Settings = Depends(get_settings),
) -> list[ShopOut]:
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
    return [_shop(item.business_id, item.campaign_ids) for item in stored]


@router.post(
    "/clients/{client_id}/shops/{shop_id}/finance-imports",
    response_model=FinanceImportOut,
)
async def import_shop_finance(
    client_id: UUID,
    shop_id: UUID,
    body: FinanceImportCreate,
    _: None = Depends(require_bootstrap),
    session: AsyncSession = Depends(get_service_session),
    settings: Settings = Depends(get_settings),
) -> FinanceImportOut:
    if settings.app_db_password == "":
        raise HTTPException(status_code=503, detail="client database is not configured")
    record = await load_client(session, client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="client not found")
    if record.status != "ready":
        raise HTTPException(status_code=409, detail="client database is not ready")
    shop = await load_shop(session, client_id, shop_id)
    if shop is None:
        raise HTTPException(status_code=404, detail="shop not found")
    if shop.connection_status != "connected":
        raise HTTPException(status_code=409, detail="shop is not connected")
    try:
        api_key = decrypt_secret(bytes(shop.api_key_ciphertext), settings.fernet_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="shop api key could not be read") from exc
    stored_campaigns = await read_tenant_campaigns(settings, record.database_name)
    campaigns = [
        ShopCampaign(campaign_id=campaign_id, placement_type=placement)
        for campaign_id, business_id, placement in stored_campaigns
        if business_id == shop.business_id
    ]
    if not campaigns:
        raise HTTPException(status_code=422, detail="shop has no campaigns")
    database_url = tenant_database_url(settings, record.database_name)
    store = SqlReportStore(database_url)
    try:
        async with tenant_import_lock(database_url, shop.id):
            result = await import_reports(
                api_key=api_key,
                base_url=settings.ym_api_base,
                business_id=shop.business_id,
                shop_id=shop.id,
                campaigns=campaigns,
                period_from=body.period_from,
                period_to=body.period_to,
                store=store,
            )
    except FinanceImportBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="finance import failed") from exc
    finally:
        await store.close()
    return FinanceImportOut(
        shop_id=shop.id,
        business_id=shop.business_id,
        period_from=body.period_from,
        period_to=body.period_to,
        version=result.version,
        reports=[
            FinanceReportOut(
                report_type=item.report_type,
                campaign_id=item.campaign_id,
                status=item.status,
                version=item.version,
                row_count=item.row_count,
                error_message=item.error_message,
                yandex_report_id=item.yandex_report_id,
            )
            for item in result.reports
        ],
    )


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
