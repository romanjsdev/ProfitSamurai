import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from profitsamurai.analytics_exclusions import ImportExclusionError, exclude_import

_MIGRATION = Path("migrations/tenant/versions/004_analytics_realization.py")


def test_blank_exclusion_text_is_rejected_before_any_database_call() -> None:
    with pytest.raises(ImportExclusionError, match="reason"):
        asyncio.run(
            exclude_import(
                "postgresql+asyncpg://unused",
                uuid4(),
                reason="  ",
                basis="basis",
            )
        )
    with pytest.raises(ImportExclusionError, match="basis"):
        asyncio.run(
            exclude_import(
                "postgresql+asyncpg://unused",
                uuid4(),
                reason="reason",
                basis="",
            )
        )


def test_selection_sql_uses_finish_time_and_names_october_versions() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "finished_at DESC NULLS LAST" in source
    assert "MAX(version)" not in source
    assert "version IN (2, 3, 4)" in source
    assert "report_type = 'goods_realization'" in source
    assert "INSERT INTO analytics.import_exclusions" not in source
    assert "DATE '2026-10-01'" in source
    assert "DATE '2026-10-31'" in source
    assert "transferred_to_delivery" in source
    assert "realization_suppressed_duplicate" in source
    assert "later.row_number - 1" in source
    assert "shopSku" not in source
    assert "yourSku" in source
