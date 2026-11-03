from sqlalchemy import BigInteger, CheckConstraint, LargeBinary, Text, UniqueConstraint, Uuid
from sqlalchemy.sql.schema import Column, Table

from profitsamurai.db.service.models import ServiceBase


def test_service_metadata_is_only_the_registry() -> None:
    assert set(ServiceBase.metadata.tables) == {"clients", "client_databases", "shops"}
    assert "cabinets" not in ServiceBase.metadata.tables
    assert "campaigns" not in ServiceBase.metadata.tables


def test_registry_columns_match_the_initial_migration() -> None:
    assert _column_names("clients") == ("id", "name")
    assert _column_names("client_databases") == ("client_id", "database_name", "status")
    assert _column_names("shops") == (
        "id",
        "client_id",
        "marketplace",
        "business_id",
        "api_key_ciphertext",
        "connection_status",
    )
    assert isinstance(_column("clients", "id").type, Uuid)
    assert isinstance(_column("clients", "name").type, Text)
    assert isinstance(_column("client_databases", "client_id").type, Uuid)
    assert isinstance(_column("client_databases", "database_name").type, Text)
    assert isinstance(_column("client_databases", "status").type, Text)
    assert isinstance(_column("shops", "id").type, Uuid)
    assert isinstance(_column("shops", "client_id").type, Uuid)
    assert isinstance(_column("shops", "marketplace").type, Text)
    assert isinstance(_column("shops", "business_id").type, BigInteger)
    assert isinstance(_column("shops", "api_key_ciphertext").type, LargeBinary)
    assert isinstance(_column("shops", "connection_status").type, Text)
    for table_name in ("clients", "client_databases", "shops"):
        table = _table(table_name)
        for column in table.columns:
            assert column.nullable is False
            assert column.server_default is None


def test_registry_constraints_keep_their_names() -> None:
    databases = _table("client_databases")
    shops = _table("shops")
    assert _check_names(databases) == {"ck_client_databases_status"}
    assert _check_names(shops) == {"ck_shops_connection_status"}
    assert _unique_names(shops) == {"uq_shops_client_marketplace_business"}
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["database_name"]
        for constraint in databases.constraints
    )
    assert {index.name for index in shops.indexes} == {"ix_shops_client_id"}
    assert _ondelete(databases, "client_id") == "CASCADE"
    assert _ondelete(shops, "client_id") == "CASCADE"


def _table(name: str) -> Table:
    table = ServiceBase.metadata.tables[name]
    assert isinstance(table, Table)
    return table


def _column(table_name: str, column_name: str) -> Column[object]:
    column = _table(table_name).c[column_name]
    assert isinstance(column, Column)
    return column


def _column_names(table_name: str) -> tuple[str, ...]:
    return tuple(column.name for column in _table(table_name).columns)


def _check_names(table: Table) -> set[str]:
    names: set[str] = set()
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str):
            names.add(constraint.name)
    return names


def _unique_names(table: Table) -> set[str]:
    names: set[str] = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and isinstance(constraint.name, str):
            names.add(constraint.name)
    return names


def _ondelete(table: Table, column_name: str) -> str | None:
    column = table.c[column_name]
    assert isinstance(column, Column)
    foreign_key = next(iter(column.foreign_keys))
    return foreign_key.ondelete
