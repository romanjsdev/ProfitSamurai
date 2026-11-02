"""Provision a client database. Set RUN_INTEGRATION=1 and the admin URL."""

import os
import re
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.config import get_settings
from profitsamurai.db.migrate import migrate_service, migrate_tenant
from profitsamurai.db.service.models import Cabinet, Client
from profitsamurai.db.service.session import create_service_engine, create_session_factory
from profitsamurai.integrations.yandex_market.campaigns import RemoteCampaign
from profitsamurai.registry import (
    ProvisionError,
    attach_shops,
    load_client,
    provision_client,
    read_tenant_campaigns,
    register_client,
)
from profitsamurai.types import BusinessId, CampaignId

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to provision a client database",
)

_NAME = re.compile(r"^client_[0-9a-f]{32}$")


async def test_provision_retries_the_same_database(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    if settings.postgres_admin_url == "":
        pytest.skip("POSTGRES_ADMIN_URL is required")
    await migrate_service(settings.database_url)
    engine = create_service_engine(settings.database_url)
    factory = create_session_factory(engine)
    client_id: UUID | None = None
    database_name: str | None = None
    business_id = 8_100_000_000 + (uuid4().int % 1_000_000)
    try:
        async with factory() as session:
            created = await register_client(session, "Acme")
            client_id = created.id
            database_name = created.database_name
        calls = {"n": 0}

        async def flaky(url: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("stopped before migrations")
            await migrate_tenant(url)

        monkeypatch.setattr("profitsamurai.registry.migrate_tenant", flaky)
        async with factory() as session:
            with pytest.raises(ProvisionError):
                await provision_client(session, settings, created.id)
            failed = await load_client(session, created.id)
        assert failed is not None
        assert failed.status == "failed"
        assert await _database_count(settings.postgres_admin_url, database_name) == 1
        assert not await _can_query(
            _role_url(
                settings,
                "profitsamurai_datalens",
                settings.datalens_db_password,
                database_name,
            )
        )

        monkeypatch.setattr("profitsamurai.registry.migrate_tenant", migrate_tenant)
        async with factory() as session:
            ready = await provision_client(session, settings, created.id)
        assert ready.status == "ready"
        assert ready.database_name == database_name
        assert await _database_count(settings.postgres_admin_url, database_name) == 1
        assert await _schema_names(settings, database_name) == ["analytics", "raw", "ref"]
        await _assert_app_role_cannot_create_database(settings)
        assert await _can_query(
            _role_url(
                settings,
                "profitsamurai_datalens",
                settings.datalens_db_password,
                database_name,
            )
        )
        assert not await _can_query(
            _role_url(
                settings,
                "profitsamurai_datalens",
                settings.datalens_db_password,
                _service_name(settings),
            )
        )
        await _assert_datalens_cannot_read_campaigns(settings, database_name)
        async with factory() as session:
            stored = await attach_shops(
                session,
                settings,
                created.id,
                "yandex-key",
                [
                    RemoteCampaign(BusinessId(business_id), CampaignId(41), "FBS"),
                    RemoteCampaign(BusinessId(business_id), CampaignId(42), "FBY"),
                ],
            )
        assert [item.campaign_ids for item in stored] == [(41, 42)]
        assert await read_tenant_campaigns(settings, database_name) == [
            (41, business_id, "FBS"),
            (42, business_id, "FBY"),
        ]
        assert await _column_names(engine, "cabinets") == ["api_key_ciphertext", "business_id"]
        assert await _column_names(engine, "campaigns") == ["business_id", "campaign_id"]
    finally:
        if database_name is not None:
            await _drop_database(settings.postgres_admin_url, database_name)
        async with factory() as session:
            if client_id is not None:
                client = await session.get(Client, client_id)
                if client is not None:
                    await session.delete(client)
            cabinet = await session.get(Cabinet, business_id)
            if cabinet is not None:
                await session.delete(cabinet)
            await session.commit()
        await engine.dispose()
        get_settings.cache_clear()


async def _database_count(admin_url: str, database_name: str) -> int:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            counted = await conn.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            )
    finally:
        await engine.dispose()
    return int(counted or 0)


async def _schema_names(settings: object, database_name: str) -> list[str]:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    url = _role_url(settings, "profitsamurai_app", settings.app_db_password, database_name)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname IN ('analytics', 'raw', 'ref') ORDER BY nspname"
                )
            )
            return [str(row[0]) for row in result]
    finally:
        await engine.dispose()


async def _column_names(engine: object, table_name: str) -> list[str]:
    from sqlalchemy.ext.asyncio import AsyncEngine

    assert isinstance(engine, AsyncEngine)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table_name "
                "ORDER BY column_name"
            ),
            {"table_name": table_name},
        )
        return [str(row[0]) for row in result]


async def _assert_app_role_cannot_create_database(settings: object) -> None:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    url = _role_url(settings, "profitsamurai_app", settings.app_db_password, "postgres")
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with pytest.raises(ProgrammingError):
            async with engine.connect() as conn:
                await conn.execute(text("CREATE DATABASE profitsamurai_app_must_not_create"))
    finally:
        await engine.dispose()


async def _assert_datalens_cannot_read_campaigns(settings: object, database_name: str) -> None:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    url = _role_url(
        settings,
        "profitsamurai_datalens",
        settings.datalens_db_password,
        database_name,
    )
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            with pytest.raises(ProgrammingError):
                await conn.execute(text("SELECT campaign_id FROM ref.campaigns"))
    finally:
        await engine.dispose()


async def _can_query(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
    finally:
        await engine.dispose()


async def _drop_database(admin_url: str, database_name: str) -> None:
    if _NAME.fullmatch(database_name) is None:
        raise AssertionError(database_name)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": database_name},
            )
            await conn.execute(text(f"DROP DATABASE IF EXISTS {database_name}"))
    finally:
        await engine.dispose()


def _role_url(settings: object, username: str, password: str, database: str) -> str:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    return (
        make_url(settings.database_url)
        .set(username=username, password=password, database=database)
        .render_as_string(hide_password=False)
    )


def _service_name(settings: object) -> str:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    name = make_url(settings.database_url).database
    assert name is not None
    return name
