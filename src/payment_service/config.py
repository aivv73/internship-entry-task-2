from functools import lru_cache

from pydantic import AnyHttpUrl, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/payments"
    )
    provider_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8081")


@lru_cache
def get_settings() -> Settings:
    return Settings()
