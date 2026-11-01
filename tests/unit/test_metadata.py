from profitsamurai.db.service.models import Cabinet, Campaign


def test_service_tables_have_only_the_identifier_columns() -> None:
    assert set(Cabinet.__table__.columns.keys()) == {"business_id", "api_key_ciphertext"}
    assert set(Campaign.__table__.columns.keys()) == {"campaign_id", "business_id"}
    assert set(Cabinet.metadata.tables) == {"cabinets", "campaigns"}
