"""Save and read a cabinet. Set RUN_INTEGRATION=1 and point DATABASE_URL at Postgres."""

import os
from uuid import uuid4

import pytest

from profitsamurai.cabinets import load_cabinet, save_cabinets
from profitsamurai.config import get_settings
from profitsamurai.db.migrate import migrate_service
from profitsamurai.db.service.models import Cabinet
from profitsamurai.db.service.session import create_service_engine, create_session_factory
from profitsamurai.integrations.yandex_market.campaigns import RemoteCampaign
from profitsamurai.types import BusinessId, CampaignId

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to write the service database",
)


async def test_save_replaces_campaign_ids() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    business_id = 8_000_000_000 + (uuid4().int % 1_000_000)
    await migrate_service(settings.database_url)
    engine = create_service_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            stored = await save_cabinets(
                session,
                fernet_key=settings.fernet_key,
                api_key="yandex-key",
                campaigns=[
                    RemoteCampaign(BusinessId(business_id), CampaignId(41)),
                    RemoteCampaign(BusinessId(business_id), CampaignId(42)),
                ],
            )
        assert [item.campaign_ids for item in stored] == [(41, 42)]
        async with factory() as session:
            loaded = await load_cabinet(session, business_id)
            assert loaded is not None
            assert loaded.campaign_ids == (41, 42)
            await save_cabinets(
                session,
                fernet_key=settings.fernet_key,
                api_key="yandex-key",
                campaigns=[RemoteCampaign(BusinessId(business_id), CampaignId(42))],
            )
            replaced = await load_cabinet(session, business_id)
            assert replaced is not None
            assert replaced.campaign_ids == (42,)
    finally:
        async with factory() as session:
            cabinet = await session.get(Cabinet, business_id)
            if cabinet is not None:
                await session.delete(cabinet)
                await session.commit()
        await engine.dispose()
        get_settings.cache_clear()
