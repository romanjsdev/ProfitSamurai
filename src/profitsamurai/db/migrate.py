"""Apply database migrations."""

import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from profitsamurai.db.tenant_guard import NAME_ENV


def _upgrade(ini_name: str, database_url: str) -> None:
    ini_path = Path.cwd() / ini_name
    if not ini_path.is_file():
        raise FileNotFoundError(f"{ini_name} was not found in {Path.cwd()}")
    config = Config(str(ini_path))
    config.set_main_option("sqlalchemy.url", database_url)
    if ini_name == "alembic.tenant.ini":
        _upgrade_named_tenant(config, database_url)
        return
    command.upgrade(config, "head")


def _upgrade_named_tenant(config: Config, database_url: str) -> None:
    parsed = make_url(database_url)
    name = parsed.database or ""
    previous = os.environ.get(NAME_ENV)
    os.environ[NAME_ENV] = name
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop(NAME_ENV, None)
        else:
            os.environ[NAME_ENV] = previous


async def migrate_service(database_url: str) -> None:
    await asyncio.to_thread(_upgrade, "alembic.service.ini", database_url)


async def migrate_tenant(database_url: str) -> None:
    await asyncio.to_thread(_upgrade, "alembic.tenant.ini", database_url)
