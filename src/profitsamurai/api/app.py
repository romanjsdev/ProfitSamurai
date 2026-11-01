"""ASGI application."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from profitsamurai.api.deps import AppState
from profitsamurai.api.routers.cabinets import router
from profitsamurai.config import get_settings
from profitsamurai.db.migrate import migrate_service
from profitsamurai.db.service.session import create_service_engine, create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    await migrate_service(settings.database_url)
    engine = create_service_engine(settings.database_url)
    app.state.container = AppState(
        settings=settings,
        session_factory=create_session_factory(engine),
    )
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="ProfitSamurai", lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()
