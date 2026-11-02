from profitsamurai.api.schemas.shops import ShopCreate, ShopOut


def test_shop_connect_keeps_the_json_field_names() -> None:
    created = ShopCreate.model_json_schema()
    assert created["title"] == "CabinetCreate"
    assert created["properties"]["api_key"]["type"] == "string"
    assert created["required"] == ["api_key"]
    response = ShopOut.model_json_schema()
    assert response["title"] == "CabinetOut"
    assert set(response["properties"]) == {"business_id", "campaign_ids"}
    assert response["required"] == ["business_id", "campaign_ids"]
