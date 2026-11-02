"""Runtime configuration."""

from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = ""
    fernet_key: str = ""
    bootstrap_token: str = ""
    ym_api_base: str = "https://api.partner.market.yandex.ru"
    postgres_admin_url: str = ""
    app_db_password: str = ""
    datalens_db_password: str = ""

    @field_validator("database_url")
    @classmethod
    def database_url_uses_asyncpg(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg driver")
        return value

    @field_validator("postgres_admin_url")
    @classmethod
    def admin_url_uses_asyncpg(cls, value: str) -> str:
        if value == "":
            return value
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("postgres_admin_url must use the postgresql+asyncpg driver")
        return value

    @field_validator("fernet_key")
    @classmethod
    def fernet_key_is_valid(cls, value: str) -> str:
        if value == "":
            raise ValueError("fernet_key is required")
        Fernet(value.encode())
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
