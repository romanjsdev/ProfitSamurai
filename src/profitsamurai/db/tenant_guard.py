"""Refuse a tenant migration unless it targets one named client database."""

import os
import re

from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL

CLIENT_DATABASE_NAME = re.compile(r"^client_[0-9a-f]{32}$")
APP_ROLE = "profitsamurai_app"
NAME_ENV = "TENANT_DATABASE_NAME"
URL_ENV = "TENANT_DATABASE_URL"


class TenantMigrationTargetError(RuntimeError):
    """Migration stopped before any revision was applied."""


def assert_tenant_target(url: str, expected_name: str) -> str:
    """Check the URL role and database name. The returned name is safe to log."""
    try:
        parsed = make_url(url)
    except Exception:
        raise TenantMigrationTargetError("tenant database url is invalid") from None
    database = parsed.database or ""
    username = parsed.username or ""
    if username != APP_ROLE:
        raise TenantMigrationTargetError("tenant migration role must be profitsamurai_app")
    if CLIENT_DATABASE_NAME.fullmatch(database) is None:
        raise TenantMigrationTargetError(
            f"tenant migration target {database!r} is not a client database"
        )
    if database != expected_name:
        raise TenantMigrationTargetError(
            "tenant migration target does not match TENANT_DATABASE_NAME"
        )
    return database


def assert_connected_database(
    current_database: str,
    current_user: str,
    expected_name: str,
) -> None:
    """Check the session after connect and before Alembic runs a revision."""
    if (
        current_user != APP_ROLE
        or current_database != expected_name
        or CLIENT_DATABASE_NAME.fullmatch(current_database) is None
    ):
        raise TenantMigrationTargetError(
            "connected database does not match the expected client database"
        )


def resolve_tenant_url(configured: str | None) -> str:
    """Choose the tenant URL and reject a service database or another client."""
    expected = os.environ.get(NAME_ENV, "")
    override = os.environ.get(URL_ENV)
    if override:
        url = override
    elif configured and _has_password(configured):
        url = configured
    else:
        url = _url_from_environment(expected)
    assert_tenant_target(url, expected)
    return url


def _has_password(url: str) -> bool:
    try:
        parsed = make_url(url)
    except Exception:
        return False
    return bool(parsed.password)


def _url_from_environment(expected: str) -> str:
    password = os.environ.get("APP_DB_PASSWORD", "")
    if password == "":
        raise TenantMigrationTargetError("APP_DB_PASSWORD is not configured")
    host = os.environ.get("TENANT_DATABASE_HOST", "postgres")
    raw_port = os.environ.get("TENANT_DATABASE_PORT", "5432")
    try:
        port = int(raw_port)
    except ValueError:
        raise TenantMigrationTargetError("TENANT_DATABASE_PORT is invalid") from None
    return URL.create(
        "postgresql+asyncpg",
        username=APP_ROLE,
        password=password,
        host=host,
        port=port,
        database=expected,
    ).render_as_string(hide_password=False)
