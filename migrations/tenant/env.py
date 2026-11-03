import asyncio
import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from profitsamurai.db.tenant.models import TenantBase  # noqa: E402
from profitsamurai.db.tenant_guard import (  # noqa: E402
    TenantMigrationTargetError,
    assert_connected_database,
    resolve_tenant_url,
)

config = context.config
target_metadata = TenantBase.metadata


def _apply_target() -> str:
    url = resolve_tenant_url(config.get_main_option("sqlalchemy.url"))
    config.set_main_option("sqlalchemy.url", url)
    return url


def run_migrations_offline() -> None:
    url = _apply_target()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    current_database = connection.scalar(text("SELECT current_database()"))
    current_user = connection.scalar(text("SELECT current_user"))
    if not isinstance(current_database, str) or not isinstance(current_user, str):
        raise TenantMigrationTargetError("connected database could not be read")
    assert_connected_database(
        current_database,
        current_user,
        os.environ.get("TENANT_DATABASE_NAME", ""),
    )
    connection.rollback()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    _apply_target()
    section = config.get_section(config.config_ini_section, {})
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
