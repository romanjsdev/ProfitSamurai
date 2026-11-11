import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_SERVICE = Path("migrations/service/versions")
_TENANT = Path("migrations/tenant/versions")


def test_service_history_has_one_root() -> None:
    script = ScriptDirectory.from_config(Config("alembic.service.ini"))
    assert script.get_heads() == ["001_registry"]
    revision = script.get_revision("001_registry")
    assert revision is not None
    assert revision.down_revision is None
    assert [path.name for path in _SERVICE.glob("*.py")] == ["001_registry.py"]


def test_tenant_history_has_one_root() -> None:
    script = ScriptDirectory.from_config(Config("alembic.tenant.ini"))
    assert script.get_heads() == ["005_analytics_services"]
    root = script.get_revision("001_tenant")
    assert root is not None
    assert root.down_revision is None
    previous = script.get_revision("003_file_sha")
    assert previous is not None
    assert previous.down_revision == "002_finance"
    previous_head = script.get_revision("004_analytics_realization")
    assert previous_head is not None
    assert previous_head.down_revision == "003_file_sha"
    head = script.get_revision("005_analytics_services")
    assert head is not None
    assert head.down_revision == "004_analytics_realization"
    assert sorted(path.name for path in _TENANT.glob("*.py")) == [
        "001_tenant_schema.py",
        "002_finance_reports.py",
        "003_file_sha.py",
        "004_analytics_realization.py",
        "005_analytics_services.py",
    ]


def test_registry_migration_does_not_call_create_all() -> None:
    module = ast.parse((_SERVICE / "001_registry.py").read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "create_all" not in calls
    assert "drop_all" not in calls
