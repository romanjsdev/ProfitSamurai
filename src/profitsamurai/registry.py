"""Register a seller company and its physical database."""

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.cabinets import StoredCabinet, save_cabinets
from profitsamurai.config import Settings
from profitsamurai.db.migrate import migrate_tenant
from profitsamurai.db.service.models import Client, ClientDatabase, Shop
from profitsamurai.db.service.session import create_service_engine, create_session_factory
from profitsamurai.db.tenant.models import TenantCampaign
from profitsamurai.integrations.yandex_market.campaigns import RemoteCampaign
from profitsamurai.security import encrypt_secret
from profitsamurai.types import CampaignId

_DATABASE_NAME = re.compile(r"^client_[0-9a-f]{32}$")
_PASSWORD = re.compile(r"^[A-Za-z0-9_]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MARKETPLACE = "yandex_market"
APP_ROLE = "profitsamurai_app"
DATALENS_ROLE = "profitsamurai_datalens"


class ProvisionError(Exception):
    """Provisioning failed. The message is safe to return to the caller."""


class ClientNotReady(ProvisionError):
    """The client database is not ready for a shop connection."""


@dataclass(frozen=True)
class ClientRecord:
    id: UUID
    name: str
    database_name: str
    status: str


def database_name_for(client_id: UUID) -> str:
    return "client_" + client_id.hex


def provisioning_configured(settings: Settings) -> bool:
    return (
        settings.postgres_admin_url != ""
        and settings.app_db_password != ""
        and settings.datalens_db_password != ""
    )


async def register_client(session: AsyncSession, name: str) -> ClientRecord:
    client_id = uuid4()
    database_name = database_name_for(client_id)
    session.add(Client(id=client_id, name=name))
    session.add(
        ClientDatabase(
            client_id=client_id,
            database_name=database_name,
            status="provisioning",
        )
    )
    await session.commit()
    return ClientRecord(
        id=client_id,
        name=name,
        database_name=database_name,
        status="provisioning",
    )


async def load_client(session: AsyncSession, client_id: UUID) -> ClientRecord | None:
    client = await session.get(Client, client_id)
    database = await session.get(ClientDatabase, client_id)
    if client is None or database is None:
        return None
    return ClientRecord(
        id=client.id,
        name=client.name,
        database_name=database.database_name,
        status=database.status,
    )


async def provision_client(
    session: AsyncSession,
    settings: Settings,
    client_id: UUID,
) -> ClientRecord:
    if not provisioning_configured(settings):
        raise ProvisionError("client database provisioning is not configured")
    current = await load_client(session, client_id)
    if current is None:
        raise LookupError(client_id)
    database = await session.get(ClientDatabase, client_id)
    if database is None:
        raise LookupError(client_id)
    name = _database_name(database.database_name)
    database.status = "provisioning"
    await session.commit()
    try:
        await ensure_cluster_roles(settings)
        await ensure_physical_database(settings.postgres_admin_url, name)
        await migrate_tenant(_tenant_url(settings, name))
        await grant_datalens(settings, name)
    except Exception as exc:
        database.status = "failed"
        await session.commit()
        await _revoke_datalens(settings.postgres_admin_url, name)
        print(f"provision failed for {name}: {type(exc).__name__}")
        if isinstance(exc, ProvisionError):
            raise
        raise ProvisionError("client database was not provisioned") from exc
    database.status = "ready"
    await session.commit()
    return ClientRecord(
        id=current.id,
        name=current.name,
        database_name=name,
        status="ready",
    )


async def attach_shops(
    session: AsyncSession,
    settings: Settings,
    client_id: UUID,
    api_key: str,
    campaigns: list[RemoteCampaign],
) -> list[StoredCabinet]:
    current = await load_client(session, client_id)
    if current is None:
        raise LookupError(client_id)
    if current.status != "ready":
        raise ClientNotReady("client database is not ready")
    stored = await save_cabinets(
        session,
        fernet_key=settings.fernet_key,
        api_key=api_key,
        campaigns=campaigns,
    )
    if not stored:
        return []
    ciphertext = encrypt_secret(api_key, settings.fernet_key)
    placements = {row.campaign_id: row.placement_type for row in campaigns}
    for cabinet in stored:
        await session.execute(
            insert(Shop)
            .values(
                id=uuid4(),
                client_id=current.id,
                marketplace=MARKETPLACE,
                business_id=cabinet.business_id,
                api_key_ciphertext=ciphertext,
                connection_status="connected",
            )
            .on_conflict_do_update(
                constraint="uq_shops_client_marketplace_business",
                set_={
                    "api_key_ciphertext": ciphertext,
                    "connection_status": "connected",
                },
            )
        )
    await session.commit()
    try:
        await _write_tenant_campaigns(settings, current.database_name, stored, placements)
    except Exception as exc:
        await session.execute(
            update(Shop)
            .where(
                Shop.client_id == current.id,
                Shop.business_id.in_([item.business_id for item in stored]),
            )
            .values(connection_status="error")
        )
        await session.commit()
        print(f"shop sync failed for {current.database_name}: {type(exc).__name__}")
        raise ProvisionError("shop campaigns were not stored") from exc
    return stored


async def ensure_cluster_roles(settings: Settings) -> None:
    app_password = _password(settings.app_db_password, "app_db_password")
    datalens_password = _password(settings.datalens_db_password, "datalens_db_password")
    service_name = _identifier(_service_database_name(settings), "service database")
    admin_user = _identifier(_username(settings.postgres_admin_url), "admin user")
    admin = _autocommit_engine(settings.postgres_admin_url)
    async with admin.connect() as conn:
        await _ensure_login(conn, APP_ROLE, app_password)
        await _ensure_login(conn, DATALENS_ROLE, datalens_password)
        await conn.execute(text(f"REVOKE CONNECT ON DATABASE {service_name} FROM PUBLIC"))
        await conn.execute(text(f"GRANT CONNECT ON DATABASE {service_name} TO {admin_user}"))
        await conn.execute(text(f"GRANT CONNECT ON DATABASE {service_name} TO {APP_ROLE}"))
    await admin.dispose()
    service_admin = _autocommit_engine(_replace_database(settings.postgres_admin_url, service_name))
    async with service_admin.connect() as conn:
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f"TO {APP_ROLE}"
            )
        )
        await conn.execute(
            text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
        )
    await service_admin.dispose()


async def ensure_physical_database(admin_url: str, database_name: str) -> None:
    name = _database_name(database_name)
    engine = _autocommit_engine(admin_url)
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": name},
        )
        if exists != 1:
            await conn.execute(text(f"CREATE DATABASE {name} OWNER {APP_ROLE}"))
        await conn.execute(text(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC"))
        await conn.execute(text(f"GRANT CONNECT, TEMP ON DATABASE {name} TO {APP_ROLE}"))
    await engine.dispose()


async def grant_datalens(settings: Settings, database_name: str) -> None:
    name = _database_name(database_name)
    admin = _autocommit_engine(settings.postgres_admin_url)
    async with admin.connect() as conn:
        await conn.execute(text(f"GRANT CONNECT ON DATABASE {name} TO {DATALENS_ROLE}"))
    await admin.dispose()
    tenant_admin = _autocommit_engine(_replace_database(settings.postgres_admin_url, name))
    async with tenant_admin.connect() as conn:
        await conn.execute(text(f"GRANT USAGE ON SCHEMA analytics TO {DATALENS_ROLE}"))
        await conn.execute(
            text(f"GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO {DATALENS_ROLE}")
        )
        await conn.execute(
            text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE "
                f"{APP_ROLE} IN SCHEMA analytics GRANT SELECT ON TABLES TO {DATALENS_ROLE}"
            )
        )
    await tenant_admin.dispose()


async def _revoke_datalens(admin_url: str, database_name: str) -> None:
    name = _database_name(database_name)
    engine = _autocommit_engine(admin_url)
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            )
            if exists != 1:
                return
            await conn.execute(text(f"REVOKE CONNECT ON DATABASE {name} FROM {DATALENS_ROLE}"))
    except SQLAlchemyError:
        return
    finally:
        await engine.dispose()


async def _write_tenant_campaigns(
    settings: Settings,
    database_name: str,
    stored: list[StoredCabinet],
    placements: dict[CampaignId, str | None],
) -> None:
    engine = create_service_engine(_tenant_url(settings, _database_name(database_name)))
    factory = create_session_factory(engine)
    try:
        async with factory() as tenant_session:
            for cabinet in stored:
                statement = insert(TenantCampaign).values(
                    [
                        {
                            "campaign_id": campaign_id,
                            "business_id": cabinet.business_id,
                            "placement_type": placements.get(campaign_id),
                        }
                        for campaign_id in cabinet.campaign_ids
                    ]
                )
                await tenant_session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[TenantCampaign.campaign_id],
                        set_={
                            "business_id": statement.excluded.business_id,
                            "placement_type": statement.excluded.placement_type,
                        },
                    )
                )
                await tenant_session.execute(
                    delete(TenantCampaign).where(
                        TenantCampaign.business_id == cabinet.business_id,
                        TenantCampaign.campaign_id.not_in(cabinet.campaign_ids),
                    )
                )
            await tenant_session.commit()
    finally:
        await engine.dispose()


def _tenant_url(settings: Settings, database_name: str) -> str:
    return _replace(
        settings.database_url,
        username=APP_ROLE,
        password=_password(settings.app_db_password, "app_db_password"),
        database=database_name,
    )


async def _ensure_login(conn: AsyncConnection, role: str, password: str) -> None:
    await conn.execute(
        text(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN PASSWORD '{password}'
                  NOSUPERUSER NOCREATEDB NOCREATEROLE;
              ELSE
                ALTER ROLE {role} WITH LOGIN PASSWORD '{password}'
                  NOSUPERUSER NOCREATEDB NOCREATEROLE;
              END IF;
            END
            $$;
            """
        )
    )


def _autocommit_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _service_database_name(settings: Settings) -> str:
    name = make_url(settings.database_url).database
    if name is None:
        raise ProvisionError("database_url has no database name")
    return name


def _username(url: str) -> str:
    username = make_url(url).username
    if username is None:
        raise ProvisionError("postgres_admin_url has no user")
    return username


def _replace_database(url: str, database: str) -> str:
    return _replace(url, database=database)


def _replace(
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> str:
    parsed = make_url(url)
    if username is not None:
        parsed = parsed.set(username=username)
    if password is not None:
        parsed = parsed.set(password=password)
    if database is not None:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


def _database_name(value: str) -> str:
    if _DATABASE_NAME.fullmatch(value) is None:
        raise ProvisionError("client database name is invalid")
    return value


def _password(value: str, label: str) -> str:
    if _PASSWORD.fullmatch(value) is None:
        raise ProvisionError(f"{label} must contain only letters, digits, and underscores")
    return value


def _identifier(value: str, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ProvisionError(f"{label} is not a safe identifier")
    return value


async def read_tenant_campaigns(
    settings: Settings,
    database_name: str,
) -> list[tuple[int, int, str | None]]:
    """Read campaign rows. Used by tests and by the connection check."""
    engine = create_service_engine(_tenant_url(settings, _database_name(database_name)))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(
                    TenantCampaign.campaign_id,
                    TenantCampaign.business_id,
                    TenantCampaign.placement_type,
                ).order_by(TenantCampaign.campaign_id)
            )
            return [
                (int(row[0]), int(row[1]), row[2] if isinstance(row[2], str) else None)
                for row in result
            ]
    finally:
        await engine.dispose()
