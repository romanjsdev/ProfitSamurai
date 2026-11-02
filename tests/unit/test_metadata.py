from profitsamurai.db.service.models import (
    Client,
    ClientDatabase,
    RegistryBase,
    ServiceBase,
    Shop,
)


def test_revision_001_metadata_has_no_tables() -> None:
    assert set(ServiceBase.metadata.tables) == set()


def test_registry_tables_are_only_the_client_model() -> None:
    assert set(RegistryBase.metadata.tables) == {"clients", "client_databases", "shops"}
    assert set(Client.__table__.columns.keys()) == {"id", "name"}
    assert set(ClientDatabase.__table__.columns.keys()) == {
        "client_id",
        "database_name",
        "status",
    }
    assert set(Shop.__table__.columns.keys()) == {
        "id",
        "client_id",
        "marketplace",
        "business_id",
        "api_key_ciphertext",
        "connection_status",
    }
