"""Apply the service-database migration."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config


def _upgrade(database_url: str) -> None:
    ini_path = Path.cwd() / "alembic.service.ini"
    if not ini_path.is_file():
        raise FileNotFoundError(f"alembic.service.ini was not found in {Path.cwd()}")
    config = Config(str(ini_path))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


async def migrate_service(database_url: str) -> None:
    await asyncio.to_thread(_upgrade, database_url)
