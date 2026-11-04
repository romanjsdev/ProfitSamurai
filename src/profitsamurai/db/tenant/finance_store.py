"""Write financial report attempts into one client's database."""

import hashlib
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from profitsamurai.db.service.session import create_service_engine, create_session_factory
from profitsamurai.db.tenant.models import ReportFile, ReportImport, ReportRow
from profitsamurai.jsonutil import JsonObject


class SqlReportStore:
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_service_engine(database_url)
        self._sessions = create_session_factory(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def next_version(
        self,
        shop_id: UUID,
        period_from: date,
        period_to: date,
    ) -> int:
        async with self._sessions() as session:
            current = await session.scalar(
                select(func.max(ReportImport.version)).where(
                    ReportImport.shop_id == shop_id,
                    ReportImport.period_from == period_from,
                    ReportImport.period_to == period_to,
                )
            )
        return int(current or 0) + 1

    async def begin(
        self,
        *,
        shop_id: UUID,
        business_id: int,
        report_type: str,
        period_from: date,
        period_to: date,
        campaign_id: int | None,
        version: int,
    ) -> UUID:
        import_id = uuid4()
        async with self._sessions() as session:
            session.add(
                ReportImport(
                    id=import_id,
                    shop_id=shop_id,
                    business_id=business_id,
                    report_type=report_type,
                    period_from=period_from,
                    period_to=period_to,
                    campaign_id=campaign_id,
                    yandex_report_id=None,
                    status="running",
                    version=version,
                    started_at=datetime.now(UTC),
                    finished_at=None,
                    row_count=0,
                    error_message=None,
                )
            )
            await session.commit()
        return import_id

    async def save_file(self, import_id: UUID, content: bytes) -> None:
        async with self._sessions() as session:
            session.add(
                ReportFile(
                    id=uuid4(),
                    import_id=import_id,
                    content=content,
                    byte_size=len(content),
                    content_sha256=hashlib.sha256(content).hexdigest(),
                )
            )
            await session.commit()

    async def save_rows(
        self,
        import_id: UUID,
        rows: list[tuple[str, JsonObject]],
    ) -> None:
        if not rows:
            return
        async with self._sessions() as session:
            session.add_all(
                [
                    ReportRow(
                        id=uuid4(),
                        import_id=import_id,
                        sheet=sheet,
                        row_number=index,
                        payload=payload,
                    )
                    for index, (sheet, payload) in enumerate(rows, start=1)
                ]
            )
            await session.commit()

    async def finish(
        self,
        import_id: UUID,
        *,
        status: str,
        row_count: int,
        error_message: str | None,
        yandex_report_id: str | None,
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(ReportImport)
                .where(ReportImport.id == import_id)
                .values(
                    status=status,
                    row_count=row_count,
                    error_message=error_message,
                    yandex_report_id=yandex_report_id,
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()
