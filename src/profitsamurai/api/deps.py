"""HTTP dependencies."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from profitsamurai.config import Settings, get_settings
from profitsamurai.security import secrets_match

bootstrap_header = APIKeyHeader(name="X-Bootstrap-Token", auto_error=False)


@dataclass
class AppState:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]


def app_state(request: Request) -> AppState:
    state = cast(object, getattr(request.app.state, "container", None))
    if not isinstance(state, AppState):
        raise RuntimeError("application is not initialized")
    return state


async def get_service_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = app_state(request).session_factory
    async with factory() as session:
        yield session


async def require_bootstrap(
    token: str | None = Depends(bootstrap_header),
    settings: Settings = Depends(get_settings),
) -> None:
    if settings.bootstrap_token == "":
        raise HTTPException(status_code=503, detail="bootstrap token is not configured")
    if token is None or not secrets_match(token, settings.bootstrap_token):
        raise HTTPException(status_code=401, detail="invalid bootstrap token")
