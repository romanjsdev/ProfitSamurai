"""Provision a client database on an isolated server.

Set RUN_INTEGRATION=1, INTEGRATION_DATABASE_URL, and INTEGRATION_ADMIN_URL.
The URLs are checked before any connection. DATABASE_URL is not a fallback.
"""

import os
import re
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.config import Settings
from profitsamurai.db.migrate import migrate_service, migrate_tenant
from profitsamurai.db.service.models import Client
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

from .isolation import IntegrationTargetError, resolve_integration_targets

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to provision a client database",
)

_NAME = re.compile(r"^client_[0-9a-f]{32}$")


async def test_provision_retries_the_same_database(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        service_url, admin_url = resolve_integration_targets(
            integration_database_url=os.getenv("INTEGRATION_DATABASE_URL", ""),
            integration_admin_url=os.getenv("INTEGRATION_ADMIN_URL", ""),
            application_database_url=os.getenv("DATABASE_URL", ""),
            application_admin_url=os.getenv("POSTGRES_ADMIN_URL", ""),
        )
    except IntegrationTargetError as exc:
        pytest.fail(str(exc))
    settings = Settings(database_url=service_url, postgres_admin_url=admin_url)
    await migrate_service(service_url)
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
        assert not await _database_privilege(
            settings.postgres_admin_url,
            "profitsamurai_datalens",
            database_name,
            "CONNECT",
        )

        monkeypatch.setattr("profitsamurai.registry.migrate_tenant", migrate_tenant)
        async with factory() as session:
            ready = await provision_client(session, settings, created.id)
        assert ready.status == "ready"
        assert ready.database_name == database_name
        assert await _database_count(settings.postgres_admin_url, database_name) == 1
        assert await _schema_names(settings, database_name) == ["analytics", "raw", "ref"]
        assert await _tenant_revision(settings, database_name) == "005_analytics_services"
        await _assert_datalens_restored(settings, database_name)
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
        assert await _public_tables(engine) >= {
            "clients",
            "client_databases",
            "shops",
            "alembic_version",
        }
        assert "cabinets" not in await _public_tables(engine)
        assert "campaigns" not in await _public_tables(engine)
    finally:
        if database_name is not None:
            await _drop_database(settings.postgres_admin_url, database_name)
        async with factory() as session:
            if client_id is not None:
                client = await session.get(Client, client_id)
                if client is not None:
                    await session.delete(client)
            await session.commit()
        await engine.dispose()


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


async def _tenant_revision(settings: object, database_name: str) -> str:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    url = _role_url(settings, "profitsamurai_app", settings.app_db_password, database_name)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM public.alembic_version"))
            rows = result.scalars()
            versions = [str(value) for value in rows]
    finally:
        await engine.dispose()
    assert versions == ["005_analytics_services"]
    return versions[0]


async def _database_privilege(
    admin_url: str,
    role: str,
    database_name: str,
    privilege: str,
) -> bool:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            allowed = await conn.scalar(
                text("SELECT has_database_privilege(:role, :database_name, :privilege)"),
                {"role": role, "database_name": database_name, "privilege": privilege},
            )
    finally:
        await engine.dispose()
    return bool(allowed)


async def _schema_privilege(
    settings: object,
    role: str,
    database_name: str,
    schema_name: str,
    privilege: str,
) -> bool:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    engine = create_async_engine(
        _role_url(settings, "profitsamurai_app", settings.app_db_password, database_name),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            allowed = await conn.scalar(
                text("SELECT has_schema_privilege(:role, :schema_name, :privilege)"),
                {"role": role, "schema_name": schema_name, "privilege": privilege},
            )
    finally:
        await engine.dispose()
    return bool(allowed)


async def _assert_datalens_restored(settings: object, database_name: str) -> None:
    from profitsamurai.config import Settings

    assert isinstance(settings, Settings)
    assert await _database_privilege(
        settings.postgres_admin_url,
        "profitsamurai_datalens",
        database_name,
        "CONNECT",
    )
    assert await _schema_privilege(
        settings, "profitsamurai_datalens", database_name, "analytics", "USAGE"
    )
    assert not await _schema_privilege(
        settings, "profitsamurai_datalens", database_name, "analytics", "CREATE"
    )
    assert not await _schema_privilege(
        settings, "profitsamurai_datalens", database_name, "raw", "USAGE"
    )
    assert not await _schema_privilege(
        settings, "profitsamurai_datalens", database_name, "ref", "USAGE"
    )


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


async def _public_tables(engine: object) -> set[str]:
    from sqlalchemy.ext.asyncio import AsyncEngine

    assert isinstance(engine, AsyncEngine)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        return {str(row[0]) for row in result}


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
