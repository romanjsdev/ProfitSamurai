"""Finance import reliability against a disposable PostgreSQL.

Set RUN_INTEGRATION=1, INTEGRATION_DATABASE_URL, and INTEGRATION_ADMIN_URL.
The URLs are checked before any connection. The working cluster is rejected.
"""

import asyncio
import io
import json
import os
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from profitsamurai.db.tenant.finance_store import SqlReportStore
from profitsamurai.finance import import_reports, tenant_import_lock
from profitsamurai.finance_recovery import (
    diagnose_running_imports,
    recover_abandoned_imports,
)
from profitsamurai.integrations.yandex_market.reports import ShopCampaign

from .isolation import IntegrationTargetError, resolve_integration_targets

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to use a disposable database",
)

_PERIOD_FROM = date(2026, 10, 1)
_PERIOD_TO = date(2026, 10, 31)
_CAMPAIGN = ShopCampaign(129958161, "FBY")
_APP_PASSWORD = "it-app-password"


@asynccontextmanager
async def _tenant() -> AsyncGenerator[tuple[str, str]]:
    try:
        _service_url, admin_url = resolve_integration_targets(
            integration_database_url=os.getenv("INTEGRATION_DATABASE_URL", ""),
            integration_admin_url=os.getenv("INTEGRATION_ADMIN_URL", ""),
            application_database_url=os.getenv("DATABASE_URL", ""),
            application_admin_url=os.getenv("POSTGRES_ADMIN_URL", ""),
        )
    except IntegrationTargetError as exc:
        pytest.fail(str(exc))
    database_name = "client_" + uuid4().hex
    await _create_database(admin_url, database_name)
    tenant_url = _app_url(admin_url, database_name)
    try:
        yield admin_url, tenant_url
    finally:
        await _drop_database(admin_url, database_name)


async def test_migration_hashes_existing_bytes_without_changing_them() -> None:
    payload = b"PK\x03\x04unchanged-report-bytes"
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "002_finance")
        import_id, file_id = uuid4(), uuid4()
        await _insert_file(tenant_url, import_id, file_id, payload)
        await asyncio.to_thread(_migrate, tenant_url, "head")
        stored, digest = await _file_and_sql_digest(tenant_url, file_id)
        assert stored == payload
        assert digest == await _column_hash(tenant_url, file_id)
        assert len(digest) == 64


async def test_versions_failures_and_abandoned_imports_keep_saved_bytes() -> None:
    async with _tenant() as (_admin_url, tenant_url):
        await asyncio.to_thread(_migrate, tenant_url, "head")
        store = SqlReportStore(tenant_url)
        shop_id = uuid4()
        try:
            import_id = await store.begin(
                shop_id=shop_id,
                business_id=10,
                report_type="goods_realization",
                period_from=_PERIOD_FROM,
                period_to=_PERIOD_TO,
                campaign_id=_CAMPAIGN.campaign_id,
                version=1,
            )
            original = b"PK\x03\x04version-one"
            await store.save_file(import_id, original)
            await store.save_rows(
                import_id,
                [("delivered", {"sku": "a"}), ("delivered", {"sku": "a"})],
            )
            before = await _file_and_sql_digest_for_import(tenant_url, import_id)
            assert before[0] == original
            assert before[1] == await _column_hash_for_import(tenant_url, import_id)

            facts = await diagnose_running_imports(tenant_url, shop_id)
            assert len(facts) == 1
            assert facts[0].classification == "abandoned"
            assert facts[0].journal_row_count == 0
            assert facts[0].saved_row_count == 2
            assert facts[0].file_saved is True

            async with tenant_import_lock(tenant_url, shop_id):
                held = await diagnose_running_imports(tenant_url, shop_id)
                assert held[0].classification == "active_or_unidentified"
                busy = await recover_abandoned_imports(tenant_url, shop_id)
                assert busy.applied is False
                assert busy.updated == 0
            still_running = await diagnose_running_imports(tenant_url, shop_id)
            assert still_running[0].classification == "abandoned"

            second = await _run_import(store, shop_id, _zip_bytes([{"sku": "b"}]))
            assert second.version == 2
            assert all(item.status == "done" for item in second.reports)
            fresh = await _imports_for_version(tenant_url, shop_id, 2)
            fresh_bytes, fresh_digest = await _file_and_sql_digest_for_import(
                tenant_url,
                fresh[0][3],
            )
            assert fresh_digest == await _column_hash_for_import(tenant_url, fresh[0][3])
            assert fresh_digest != before[1]
            assert fresh_bytes != original
            unchanged, unchanged_digest = await _file_and_sql_digest_for_import(
                tenant_url,
                import_id,
            )
            assert unchanged == original
            assert unchanged_digest == before[1]
            assert await _status(tenant_url, import_id) == "running"

            recovered = await recover_abandoned_imports(tenant_url, shop_id)
            assert recovered.applied is True
            assert recovered.updated == 1
            assert await _status(tenant_url, import_id) == "failed"
            assert await _journal_count(tenant_url, import_id) == 2
            assert await _saved_count(tenant_url, import_id) == 2
            after, after_digest = await _file_and_sql_digest_for_import(tenant_url, import_id)
            assert after == original
            assert after_digest == before[1]
            assert (await _version_statuses(tenant_url, shop_id))[2] == ["done", "done", "done"]

            broken = _FinishOnce(tenant_url)
            failed = await _run_import(
                broken,
                shop_id,
                _zip_bytes([{"sku": "c"}, {"sku": "c"}]),
            )
            assert failed.version == 3
            assert failed.reports[0].status == "failed"
            assert failed.reports[0].error_message == "report finalize failed"
            assert failed.reports[0].row_count == 2
            version_three = await _imports_for_version(tenant_url, shop_id, 3)
            goods = next(item for item in version_three if item[0] == "goods_realization")
            assert goods[1] == "failed"
            assert goods[2] == 2
            assert await _saved_count(tenant_url, goods[3]) == 2

            bad = await _run_import(store, shop_id, _zip_bytes([{"sku": "d"}, "not-a-row"]))
            assert bad.reports[0].status == "failed"
            assert bad.reports[0].error_message is not None
            assert "delivered.json element 2" in bad.reports[0].error_message
            assert "not-a-row" not in bad.reports[0].error_message
            assert bad.reports[0].row_count == 0
            assert (
                await _saved_count_for_version(
                    tenant_url,
                    shop_id,
                    bad.version,
                    "goods_realization",
                )
                == 0
            )
            assert (
                await _file_count_for_version(
                    tenant_url,
                    shop_id,
                    bad.version,
                    "goods_realization",
                )
                == 1
            )
        finally:
            await store.close()


class _FinishOnce(SqlReportStore):
    def __init__(self, database_url: str) -> None:
        super().__init__(database_url)
        self._failed = False

    async def finish(
        self,
        import_id: UUID,
        *,
        status: str,
        row_count: int,
        error_message: str | None,
        yandex_report_id: str | None,
    ) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("finish failed")
        await super().finish(
            import_id,
            status=status,
            row_count=row_count,
            error_message=error_message,
            yandex_report_id=yandex_report_id,
        )


def _migrate(database_url: str, revision: str) -> None:
    parsed = make_url(database_url)
    previous = os.environ.get("TENANT_DATABASE_NAME")
    os.environ["TENANT_DATABASE_NAME"] = parsed.database or ""
    config = Config("alembic.tenant.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("TENANT_DATABASE_NAME", None)
        else:
            os.environ["TENANT_DATABASE_NAME"] = previous


async def _run_import(store: SqlReportStore, shop_id: UUID, archive: bytes):
    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"status": "OK", "result": {"reportId": "rep-1"}})
        return httpx.Response(
            200,
            json={"status": "OK", "result": {"status": "DONE", "file": "https://cdn.example/r.zip"}},
        )

    def download(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=archive)

    async def sleep(_seconds: float) -> None:
        return None

    return await import_reports(
        api_key="test-api-key",
        base_url="https://api.partner.market.yandex.ru",
        business_id=10,
        shop_id=shop_id,
        campaigns=[_CAMPAIGN],
        period_from=_PERIOD_FROM,
        period_to=_PERIOD_TO,
        store=store,
        transport=httpx.MockTransport(api),
        download_transport=httpx.MockTransport(download),
        timeout_seconds=1,
        pause_seconds=0,
        gap_seconds=0,
        sleep=sleep,
        attempts=1,
    )


def _zip_bytes(rows: list[object]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("delivered.json", json.dumps(rows))
    return buffer.getvalue()


def _app_url(admin_url: str, database_name: str) -> str:
    url = make_url(admin_url).set(
        drivername="postgresql+asyncpg",
        username="profitsamurai_app",
        password=_APP_PASSWORD,
        database=database_name,
    )
    return url.render_as_string(hide_password=False)


async def _create_database(admin_url: str, database_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        CREATE ROLE profitsamurai_app LOGIN PASSWORD 'it-app-password';
                    EXCEPTION
                        WHEN duplicate_object THEN
                            ALTER ROLE profitsamurai_app PASSWORD 'it-app-password';
                    END
                    $$
                    """
                )
            )
            await conn.execute(text(f'CREATE DATABASE "{database_name}" OWNER profitsamurai_app'))
    finally:
        await engine.dispose()
    extension = create_async_engine(
        make_url(admin_url).set(database=database_name).render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with extension.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    finally:
        await extension.dispose()


async def _drop_database(admin_url: str, database_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :name AND pid <> pg_backend_pid()
                    """
                ),
                {"name": database_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        await engine.dispose()


async def _insert_file(database_url: str, import_id: UUID, file_id: UUID, payload: bytes) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO raw.report_imports (
                        id, shop_id, business_id, report_type, period_from, period_to,
                        campaign_id, yandex_report_id, status, version, started_at,
                        finished_at, row_count, error_message
                    ) VALUES (
                        :id, :shop_id, 10, 'goods_realization', DATE '2026-10-01',
                        DATE '2026-10-31', 129958161, NULL, 'done', 1, now(), now(), 0, NULL
                    )
                    """
                ),
                {"id": import_id, "shop_id": uuid4()},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO raw.report_files (id, import_id, content, byte_size)
                    VALUES (:id, :import_id, :content, :byte_size)
                    """
                ),
                {
                    "id": file_id,
                    "import_id": import_id,
                    "content": payload,
                    "byte_size": len(payload),
                },
            )
    finally:
        await engine.dispose()


async def _file_and_sql_digest(database_url: str, file_id: UUID) -> tuple[bytes, str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT content, encode(digest(content, 'sha256'), 'hex')
                        FROM raw.report_files
                        WHERE id = :id
                        """
                    ),
                    {"id": file_id},
                )
            ).one()
    finally:
        await engine.dispose()
    content = row[0] if isinstance(row[0], bytes) else bytes(row[0])
    return content, str(row[1])


async def _column_hash(database_url: str, file_id: UUID) -> str:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT content_sha256 FROM raw.report_files WHERE id = :id"),
                {"id": file_id},
            )
    finally:
        await engine.dispose()
    return str(value)


async def _file_and_sql_digest_for_import(database_url: str, import_id: UUID) -> tuple[bytes, str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT content, encode(digest(content, 'sha256'), 'hex')
                        FROM raw.report_files
                        WHERE import_id = :id
                        """
                    ),
                    {"id": import_id},
                )
            ).one()
    finally:
        await engine.dispose()
    content = row[0] if isinstance(row[0], bytes) else bytes(row[0])
    return content, str(row[1])


async def _column_hash_for_import(database_url: str, import_id: UUID) -> str:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT content_sha256 FROM raw.report_files WHERE import_id = :id"),
                {"id": import_id},
            )
    finally:
        await engine.dispose()
    return str(value)


async def _status(database_url: str, import_id: UUID) -> str:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT status FROM raw.report_imports WHERE id = :id"),
                {"id": import_id},
            )
    finally:
        await engine.dispose()
    return str(value)


async def _journal_count(database_url: str, import_id: UUID) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT row_count FROM raw.report_imports WHERE id = :id"),
                {"id": import_id},
            )
    finally:
        await engine.dispose()
    return int(value or 0)


async def _saved_count(database_url: str, import_id: UUID) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT count(*) FROM raw.report_rows WHERE import_id = :id"),
                {"id": import_id},
            )
    finally:
        await engine.dispose()
    return int(value or 0)


async def _version_statuses(database_url: str, shop_id: UUID) -> dict[int, list[str]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT version, status
                    FROM raw.report_imports
                    WHERE shop_id = :shop_id
                    ORDER BY version, report_type
                    """
                ),
                {"shop_id": shop_id},
            )
            rows = result.all()
    finally:
        await engine.dispose()
    grouped: dict[int, list[str]] = {}
    for version, status in rows:
        grouped.setdefault(int(version), []).append(str(status))
    return grouped


async def _imports_for_version(
    database_url: str,
    shop_id: UUID,
    version: int,
) -> list[tuple[str, str, int, UUID]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT report_type, status, row_count, id
                    FROM raw.report_imports
                    WHERE shop_id = :shop_id AND version = :version
                    """
                ),
                {"shop_id": shop_id, "version": version},
            )
            rows = result.all()
    finally:
        await engine.dispose()
    return [(str(item[0]), str(item[1]), int(item[2]), item[3]) for item in rows]


async def _saved_count_for_version(
    database_url: str,
    shop_id: UUID,
    version: int,
    report_type: str,
) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM raw.report_rows AS r
                    JOIN raw.report_imports AS i ON i.id = r.import_id
                    WHERE i.shop_id = :shop_id
                      AND i.version = :version
                      AND i.report_type = :report_type
                    """
                ),
                {"shop_id": shop_id, "version": version, "report_type": report_type},
            )
    finally:
        await engine.dispose()
    return int(value or 0)


async def _file_count_for_version(
    database_url: str,
    shop_id: UUID,
    version: int,
    report_type: str,
) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM raw.report_files AS f
                    JOIN raw.report_imports AS i ON i.id = f.import_id
                    WHERE i.shop_id = :shop_id
                      AND i.version = :version
                      AND i.report_type = :report_type
                    """
                ),
                {"shop_id": shop_id, "version": version, "report_type": report_type},
            )
    finally:
        await engine.dispose()
    return int(value or 0)
