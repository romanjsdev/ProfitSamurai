import asyncio
import os

import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from profitsamurai.db.tenant_guard import (
    TenantMigrationTargetError,
    assert_connected_database,
    assert_tenant_target,
    resolve_tenant_url,
)

CLIENT_A = "client_7fa5a7ae7619407ba599c5456571750d"
CLIENT_B = "client_8796a34e11c64478b24f3431810bc3f2"
PASSWORD = "app-password-not-for-logs"


def _url(database: str, username: str = "profitsamurai_app", password: str = PASSWORD) -> str:
    return f"postgresql+asyncpg://{username}:{password}@postgres:5432/{database}"


def test_expected_client_database_is_accepted() -> None:
    assert assert_tenant_target(_url(CLIENT_A), CLIENT_A) == CLIENT_A


def test_service_database_is_rejected_and_password_is_not_in_the_error() -> None:
    with pytest.raises(TenantMigrationTargetError) as caught:
        assert_tenant_target(_url("profitsamurai_service"), CLIENT_A)
    assert PASSWORD not in str(caught.value)
    assert "profitsamurai_service" in str(caught.value)


def test_another_client_database_is_rejected() -> None:
    with pytest.raises(TenantMigrationTargetError, match="TENANT_DATABASE_NAME"):
        assert_tenant_target(_url(CLIENT_B), CLIENT_A)


def test_superuser_role_is_rejected() -> None:
    with pytest.raises(TenantMigrationTargetError, match="profitsamurai_app"):
        assert_tenant_target(_url(CLIENT_A, username="profitsamurai"), CLIENT_A)


def test_placeholder_ini_url_uses_the_named_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_DATABASE_NAME", CLIENT_A)
    monkeypatch.delenv("TENANT_DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("TENANT_DATABASE_HOST", "postgres")
    url = resolve_tenant_url("postgresql+asyncpg://profitsamurai_app@localhost/client_example")
    parsed = make_url(url)
    assert parsed.database == CLIENT_A
    assert parsed.username == "profitsamurai_app"
    assert parsed.host == "postgres"
    assert parsed.password == PASSWORD


def test_explicit_url_for_another_database_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_DATABASE_NAME", CLIENT_A)
    monkeypatch.setenv("TENANT_DATABASE_URL", _url(CLIENT_B))
    with pytest.raises(TenantMigrationTargetError) as caught:
        resolve_tenant_url(_url(CLIENT_A))
    assert PASSWORD not in str(caught.value)


def test_connected_session_must_match_the_expected_name() -> None:
    assert_connected_database(CLIENT_A, "profitsamurai_app", CLIENT_A)
    with pytest.raises(TenantMigrationTargetError):
        assert_connected_database("profitsamurai_service", "profitsamurai_app", CLIENT_A)
    with pytest.raises(TenantMigrationTargetError):
        assert_connected_database(CLIENT_A, "profitsamurai", CLIENT_A)


def test_migrate_tenant_publishes_the_database_name_without_keeping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_upgrade(config: Config, revision: str) -> None:
        del revision
        captured["name"] = os.environ["TENANT_DATABASE_NAME"]
        configured = config.get_main_option("sqlalchemy.url")
        assert configured is not None
        captured["database"] = make_url(configured).database or ""

    monkeypatch.setattr("profitsamurai.db.migrate.command.upgrade", fake_upgrade)
    monkeypatch.delenv("TENANT_DATABASE_NAME", raising=False)
    from profitsamurai.db.migrate import migrate_tenant

    asyncio.run(migrate_tenant(_url(CLIENT_A)))
    assert captured == {"name": CLIENT_A, "database": CLIENT_A}
    assert "TENANT_DATABASE_NAME" not in os.environ
