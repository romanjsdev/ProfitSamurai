"""Request bodies and responses for client registration."""

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if stripped == "":
            raise ValueError("name is empty")
        return stripped


class ClientOut(BaseModel):
    id: UUID
    name: str
    database_name: str
    status: str
