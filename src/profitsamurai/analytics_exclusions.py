"""Keep one import out of analytics without changing its raw rows or file."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine

from profitsamurai.db.service.session import create_service_engine, create_session_factory
from profitsamurai.db.tenant.models import ImportExclusion, ReportImport


class ImportExclusionError(Exception):
    """The exclusion could not be recorded."""


async def exclude_import(
    database_url: str,
    import_id: UUID,
    *,
    reason: str,
    basis: str,
) -> UUID:
    """Record an active exclusion. The import, its file, and its rows stay as they are."""
    stored_reason = _required_text(reason, "reason")
    stored_basis = _required_text(basis, "basis")
    engine = create_service_engine(database_url)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            found = await session.scalar(
                select(ReportImport.id).where(ReportImport.id == import_id)
            )
            if found is None:
                raise ImportExclusionError("import was not found")
            active = await session.scalar(
                select(ImportExclusion.id).where(
                    ImportExclusion.import_id == import_id,
                    ImportExclusion.lifted_at.is_(None),
                )
            )
            if active is not None:
                raise ImportExclusionError("import already has an active exclusion")
            exclusion_id = uuid4()
            session.add(
                ImportExclusion(
                    id=exclusion_id,
                    import_id=import_id,
                    reason=stored_reason,
                    basis=stored_basis,
                )
            )
            await session.commit()
            return exclusion_id
    finally:
        await engine.dispose()


async def lift_import_exclusion(database_url: str, import_id: UUID) -> bool:
    """Lift the active exclusion so the import can be selected again."""
    engine: AsyncEngine = create_service_engine(database_url)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            result = await session.execute(
                update(ImportExclusion)
                .where(
                    ImportExclusion.import_id == import_id,
                    ImportExclusion.lifted_at.is_(None),
                )
                .values(lifted_at=datetime.now(UTC))
            )
            await session.commit()
            if not isinstance(result, CursorResult):
                return False
            return result.rowcount == 1
    finally:
        await engine.dispose()


def _required_text(value: str, name: str) -> str:
    cleaned = value.strip()
    if cleaned == "":
        raise ImportExclusionError(f"{name} is required")
    return cleaned
