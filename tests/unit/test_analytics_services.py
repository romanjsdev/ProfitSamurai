from pathlib import Path

_MIGRATION = Path("migrations/tenant/versions/005_analytics_services.py")


def test_service_sql_keeps_source_fields_and_does_not_invent_keys() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "analytics.selected_import" in source
    assert "MAX(version)" not in source
    assert "yourSku" not in source
    assert "WHEN 'placement' THEN " in source
    assert "totalAmount" in source
    assert "paidStorage" in source
    assert "servicePrice" in source
    assert "serviceDateTime" in source
    assert "serviceDate" in source
    assert "bank_order_conflict" in source
    assert "transactionId" in source
    assert "realization_suppressed_duplicate" not in source
    assert "INSERT INTO" not in source
