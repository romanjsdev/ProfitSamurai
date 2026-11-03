import pytest

from ..integration.isolation import IntegrationTargetError, resolve_integration_targets

_SERVICE = "postgresql+asyncpg://it_user:secret@127.0.0.1:54329/profitsamurai_it_local"
_ADMIN = "postgresql+asyncpg://it_user:secret@127.0.0.1:54329/postgres"
_LIVE = "postgresql+asyncpg://app:secret@127.0.0.1:5432/profitsamurai_service"
_LIVE_ADMIN = "postgresql+asyncpg://app:secret@127.0.0.1:5432/postgres"


def test_isolated_target_is_accepted() -> None:
    assert resolve_integration_targets(
        integration_database_url=_SERVICE,
        integration_admin_url=_ADMIN,
        application_database_url="",
        application_admin_url="",
    ) == (_SERVICE, _ADMIN)


@pytest.mark.parametrize(
    ("service", "admin"),
    [
        ("", _ADMIN),
        (_SERVICE, ""),
        ("postgresql+asyncpg://it_user:secret@127.0.0.1:54329/profitsamurai_service", _ADMIN),
        ("postgresql+asyncpg://it_user:secret@127.0.0.1:54329/postgres", _ADMIN),
        ("postgresql+asyncpg://it_user:secret@127.0.0.1:54329/client_abc", _ADMIN),
        ("postgresql+asyncpg://it_user:secret@127.0.0.1:54329/profitsamurai_schema_check", _ADMIN),
        (_SERVICE, "postgresql+asyncpg://it_user:secret@127.0.0.1:54329/profitsamurai_it_local"),
        (
            "postgresql+asyncpg://it_user:secret@127.0.0.1:5432/profitsamurai_it_local",
            "postgresql+asyncpg://it_user:secret@127.0.0.1:5432/postgres",
        ),
        (
            "postgresql+asyncpg://it_user:secret@postgres:5432/profitsamurai_it_local",
            "postgresql+asyncpg://it_user:secret@postgres:5432/postgres",
        ),
    ],
)
def test_protected_targets_fail_before_use(service: str, admin: str) -> None:
    with pytest.raises(IntegrationTargetError):
        resolve_integration_targets(
            integration_database_url=service,
            integration_admin_url=admin,
            application_database_url="",
            application_admin_url="",
        )


def test_application_server_cannot_be_reused() -> None:
    with pytest.raises(IntegrationTargetError, match="DATABASE_URL"):
        resolve_integration_targets(
            integration_database_url=_LIVE.replace("5432", "54329").replace(
                "profitsamurai_service", "profitsamurai_it_local"
            ),
            integration_admin_url=_LIVE_ADMIN.replace("5432", "54329"),
            application_database_url=_LIVE.replace("5432", "54329"),
            application_admin_url="",
        )


def test_application_admin_server_cannot_be_reused() -> None:
    with pytest.raises(IntegrationTargetError, match="POSTGRES_ADMIN_URL"):
        resolve_integration_targets(
            integration_database_url=_SERVICE,
            integration_admin_url=_ADMIN,
            application_database_url="",
            application_admin_url=_ADMIN,
        )


def test_errors_do_not_include_the_secret() -> None:
    with pytest.raises(IntegrationTargetError) as caught:
        resolve_integration_targets(
            integration_database_url="not a url secret-token",
            integration_admin_url=_ADMIN,
            application_database_url="",
            application_admin_url="",
        )
    assert "secret-token" not in str(caught.value)
