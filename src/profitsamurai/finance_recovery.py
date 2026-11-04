"""Find abandoned finance imports without mistaking a live task for a stuck one.

The shop advisory lock is held for the whole import and released when that task
returns or its database session ends. Duration is not a signal: a report can
wait minutes for Yandex between files and still be healthy.

The journal does not record which import id owns the lock. While the lock is
held, every ``running`` row for that shop is unclassified and must stay as it
is. While the lock is free, no import task for that shop is alive, so every
``running`` row is abandoned.

Recovery is explicit. It takes the same lock, changes nothing if the lock is
busy, and never starts another import. For abandoned rows it sets ``failed``,
sets ``row_count`` to the number of saved rows, and leaves the ZIP and rows in
place. A full repair of a half-written attempt still needs this explicit call:
nothing in the import itself rewrites an older ``running`` row.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.finance import advisory_keys

ABANDONED_IMPORT = "import stopped before finish; saved file and rows were kept"
ACTIVE_IMPORT = "shop import is active"
RECOVERED_IMPORT = "abandoned imports marked failed"


@dataclass(frozen=True)
class RunningImportFact:
    import_id: UUID
    shop_id: UUID
    started_at: datetime
    journal_row_count: int
    saved_row_count: int
    file_saved: bool
    shop_lock_held: bool
    classification: str


@dataclass(frozen=True)
class AbandonedRecovery:
    applied: bool
    updated: int
    reason: str


def classify_running_import(*, shop_lock_held: bool) -> str:
    """Return ``active_or_unidentified`` or ``abandoned``. Age is not used."""
    if shop_lock_held:
        return "active_or_unidentified"
    return "abandoned"


def advisory_lock_ids(shop_id: UUID) -> tuple[int, int]:
    """Unsigned lock ids as ``pg_locks`` stores the signed advisory keys."""
    first, second = advisory_keys(shop_id)
    return first & 0xFFFFFFFF, second & 0xFFFFFFFF


async def shop_import_lock_held(database_url: str, shop_id: UUID) -> bool:
    first, second = advisory_lock_ids(shop_id)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text(
                    """
                    SELECT 1
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND classid = CAST(:first AS oid)
                      AND objid = CAST(:second AS oid)
                      AND objsubid = 2
                      AND granted
                    LIMIT 1
                    """
                ),
                {"first": first, "second": second},
            )
    finally:
        await engine.dispose()
    return found is not None


async def diagnose_running_imports(
    database_url: str,
    shop_id: UUID,
) -> list[RunningImportFact]:
    """Read ``running`` attempts. This does not update them."""
    lock_held = await shop_import_lock_held(database_url, shop_id)
    classification = classify_running_import(shop_lock_held=lock_held)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        i.id,
                        i.shop_id,
                        i.started_at,
                        i.row_count,
                        (
                            SELECT count(*)
                            FROM raw.report_rows AS r
                            WHERE r.import_id = i.id
                        ) AS saved_row_count,
                        EXISTS (
                            SELECT 1
                            FROM raw.report_files AS f
                            WHERE f.import_id = i.id
                        ) AS file_saved
                    FROM raw.report_imports AS i
                    WHERE i.shop_id = :shop_id
                      AND i.status = 'running'
                    ORDER BY i.started_at, i.id
                    """
                ),
                {"shop_id": shop_id},
            )
            rows = result.mappings().all()
    finally:
        await engine.dispose()
    return [_running_fact(row, lock_held=lock_held, classification=classification) for row in rows]


def _running_fact(
    row: object,
    *,
    lock_held: bool,
    classification: str,
) -> RunningImportFact:
    if not hasattr(row, "__getitem__"):
        raise TypeError("running import row could not be read")
    import_id = row["id"]  # type: ignore[index]
    shop_id = row["shop_id"]  # type: ignore[index]
    started_at = row["started_at"]  # type: ignore[index]
    journal_row_count = row["row_count"]  # type: ignore[index]
    saved_row_count = row["saved_row_count"]  # type: ignore[index]
    file_saved = row["file_saved"]  # type: ignore[index]
    if (
        not isinstance(import_id, UUID)
        or not isinstance(shop_id, UUID)
        or not isinstance(started_at, datetime)
        or isinstance(journal_row_count, bool)
        or not isinstance(journal_row_count, int)
        or isinstance(saved_row_count, bool)
        or not isinstance(saved_row_count, int)
        or not isinstance(file_saved, bool)
    ):
        raise TypeError("running import row could not be read")
    return RunningImportFact(
        import_id=import_id,
        shop_id=shop_id,
        started_at=started_at,
        journal_row_count=journal_row_count,
        saved_row_count=saved_row_count,
        file_saved=file_saved,
        shop_lock_held=lock_held,
        classification=classification,
    )


async def recover_abandoned_imports(database_url: str, shop_id: UUID) -> AbandonedRecovery:
    """Mark abandoned ``running`` rows failed, without deleting saved data.

    If the shop lock is already held, the call changes nothing: an active task
    cannot be separated from an older ``running`` row.
    """
    first, second = advisory_keys(shop_id)
    engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            locked = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:first, :second)"),
                {"first": first, "second": second},
            )
            if not locked:
                return AbandonedRecovery(applied=False, updated=0, reason=ACTIVE_IMPORT)
            try:
                updated = await _mark_abandoned(database_url, shop_id)
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:first, :second)"),
                    {"first": first, "second": second},
                )
    finally:
        await engine.dispose()
    return AbandonedRecovery(applied=True, updated=updated, reason=RECOVERED_IMPORT)


async def _mark_abandoned(database_url: str, shop_id: UUID) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE raw.report_imports AS i
                    SET status = 'failed',
                        row_count = (
                            SELECT count(*)::integer
                            FROM raw.report_rows AS r
                            WHERE r.import_id = i.id
                        ),
                        error_message = :message,
                        finished_at = now()
                    WHERE i.shop_id = :shop_id
                      AND i.status = 'running'
                    RETURNING i.id
                    """
                ),
                {"shop_id": shop_id, "message": ABANDONED_IMPORT},
            )
            updated = len(result.scalars().all())
    finally:
        await engine.dispose()
    return updated
