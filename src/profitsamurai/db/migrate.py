"""Apply database migrations."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config


def _upgrade(ini_name: str, database_url: str) -> None:
    ini_path = Path.cwd() / ini_name
    if not ini_path.is_file():
        raise FileNotFoundError(f"{ini_name} was not found in {Path.cwd()}")
    config = Config(str(ini_path))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


async def migrate_service(database_url: str) -> None:
    await asyncio.to_thread(_upgrade, "alembic.service.ini", database_url)


async def migrate_tenant(database_url: str) -> None:
    await asyncio.to_thread(_upgrade, "alembic.tenant.ini", database_url)
