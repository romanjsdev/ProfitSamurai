import ast
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from profitsamurai.db.tenant_guard import TenantMigrationTargetError, assert_connected_database

CLIENT_A = "client_7fa5a7ae7619407ba599c5456571750d"
_ENV = Path("migrations/tenant/env.py")


class _Probe:
    def __init__(self, database: str, user: str) -> None:
        self.database = database
        self.user = user
        self.order: list[str] = []

    def scalar(self, statement: object) -> str:
        sql = getattr(statement, "text", "")
        if not isinstance(sql, str):
            sql = str(statement)
        if "current_database" in sql:
            self.order.append("current_database")
            return self.database
        if "current_user" in sql:
            self.order.append("current_user")
            return self.user
        raise AssertionError(sql)

    def rollback(self) -> None:
        self.order.append("rollback")


class _Begin:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def __enter__(self) -> "_Begin":
        self._order.append("begin_transaction")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _Context:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def configure(self, **kwargs: object) -> None:
        del kwargs
        self._order.append("configure")

    def begin_transaction(self) -> _Begin:
        return _Begin(self._order)

    def run_migrations(self) -> None:
        self._order.append("run_migrations")


def _load_do_run_migrations(
    context: _Context,
    checker: Callable[[str, str, str], None],
) -> Callable[[Connection], None]:
    tree = ast.parse(_ENV.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "do_run_migrations"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace: dict[str, object] = {
        "Connection": Connection,
        "TenantMigrationTargetError": TenantMigrationTargetError,
        "assert_connected_database": checker,
        "context": context,
        "os": os,
        "target_metadata": None,
        "text": text,
    }
    exec(compile(module, filename=str(_ENV), mode="exec"), namespace)
    loaded = namespace["do_run_migrations"]
    if not callable(loaded):
        raise AssertionError("do_run_migrations was not loaded")
    return cast(Callable[[Connection], None], loaded)


def test_inspection_transaction_ends_before_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_DATABASE_NAME", CLIENT_A)
    probe = _Probe(CLIENT_A, "profitsamurai_app")

    def checker(database: str, user: str, expected: str) -> None:
        probe.order.append("check")
        assert_connected_database(database, user, expected)

    runner = _load_do_run_migrations(_Context(probe.order), checker)
    runner(cast(Connection, probe))
    assert probe.order == [
        "current_database",
        "current_user",
        "check",
        "rollback",
        "configure",
        "begin_transaction",
        "run_migrations",
    ]


def test_rejected_database_does_not_start_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_DATABASE_NAME", CLIENT_A)
    probe = _Probe("profitsamurai_service", "profitsamurai_app")

    def checker(database: str, user: str, expected: str) -> None:
        probe.order.append("check")
        assert_connected_database(database, user, expected)

    runner = _load_do_run_migrations(_Context(probe.order), checker)
    with pytest.raises(TenantMigrationTargetError):
        runner(cast(Connection, probe))
    assert probe.order == ["current_database", "current_user", "check"]
