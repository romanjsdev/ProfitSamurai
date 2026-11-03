"""Reject integration targets before any connection is opened."""

import re

from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import ArgumentError

_IT_NAME = r"^profitsamurai_it_[a-z0-9_]+$"
_WORKING_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
_WORKING_PORT = 5432


class IntegrationTargetError(RuntimeError):
    """The integration URLs are missing or point at a protected database."""


def resolve_integration_targets(
    *,
    integration_database_url: str,
    integration_admin_url: str,
    application_database_url: str,
    application_admin_url: str,
) -> tuple[str, str]:
    """Return the service and admin URLs that tests are allowed to change."""
    if integration_database_url == "" or integration_admin_url == "":
        raise IntegrationTargetError(
            "INTEGRATION_DATABASE_URL and INTEGRATION_ADMIN_URL are required"
        )
    service = _parse("INTEGRATION_DATABASE_URL", integration_database_url)
    admin = _parse("INTEGRATION_ADMIN_URL", integration_admin_url)
    _require_service_database(service)
    if admin.database != "postgres":
        raise IntegrationTargetError(
            "INTEGRATION_ADMIN_URL must use the postgres maintenance database"
        )
    if _endpoint(service) != _endpoint(admin):
        raise IntegrationTargetError(
            "INTEGRATION_ADMIN_URL must target the same server as INTEGRATION_DATABASE_URL"
        )
    if _is_working_cluster(_endpoint(service)):
        raise IntegrationTargetError(
            "integration URLs must not use localhost, 127.0.0.1, or postgres on port 5432"
        )
    _reject_application_server("DATABASE_URL", application_database_url, _endpoint(service))
    _reject_application_server(
        "POSTGRES_ADMIN_URL",
        application_admin_url,
        _endpoint(admin),
    )
    return integration_database_url, integration_admin_url


def _require_service_database(service: URL) -> None:
    name = service.database or ""
    if name in {"profitsamurai_service", "postgres"} or name.startswith("client_"):
        raise IntegrationTargetError(
            "integration database must not be profitsamurai_service, postgres, or client_*"
        )
    if re.fullmatch(_IT_NAME, name) is None:
        raise IntegrationTargetError(
            "integration database name must match profitsamurai_it_[a-z0-9_]+"
        )


def _parse(label: str, value: str) -> URL:
    try:
        parsed = make_url(value)
    except ArgumentError:
        raise IntegrationTargetError(f"{label} is not a usable database URL") from None
    if parsed.drivername != "postgresql+asyncpg":
        raise IntegrationTargetError(f"{label} must use the postgresql+asyncpg driver")
    if not parsed.database:
        raise IntegrationTargetError(f"{label} must name a database")
    if not parsed.host:
        raise IntegrationTargetError(f"{label} must name a host")
    return parsed


def _endpoint(url: URL) -> tuple[str, int]:
    return (url.host or "").lower(), url.port or 5432


def _is_working_cluster(endpoint: tuple[str, int]) -> bool:
    host, port = endpoint
    return host in _WORKING_HOSTS and port == _WORKING_PORT


def _reject_application_server(label: str, value: str, integration: tuple[str, int]) -> None:
    if value == "":
        return
    parsed = _parse(label, value)
    if _endpoint(parsed) == integration:
        raise IntegrationTargetError(
            f"integration server must differ from the server in {label}"
        )
