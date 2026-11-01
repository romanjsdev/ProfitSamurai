"""Request bodies and responses for the cabinet endpoints."""

from pydantic import BaseModel, Field, field_validator


class CabinetCreate(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def strip_key(cls, value: str) -> str:
        stripped = value.strip()
        if stripped == "":
            raise ValueError("api_key is empty")
        return stripped


class CabinetOut(BaseModel):
    business_id: int
    campaign_ids: list[int]
