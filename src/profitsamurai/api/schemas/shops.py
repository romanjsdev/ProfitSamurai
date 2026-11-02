"""JSON contract for connecting a shop. Field names match the previous body."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ShopCreate(BaseModel):
    model_config = ConfigDict(title="CabinetCreate")

    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def strip_key(cls, value: str) -> str:
        stripped = value.strip()
        if stripped == "":
            raise ValueError("api_key is empty")
        return stripped


class ShopOut(BaseModel):
    model_config = ConfigDict(title="CabinetOut")

    business_id: int
    campaign_ids: list[int]
