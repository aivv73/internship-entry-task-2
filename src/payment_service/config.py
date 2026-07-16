from functools import lru_cache
from typing import Annotated, Self

from pydantic import AnyHttpUrl, Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TimingSeconds = Annotated[float, Field(gt=0, le=86_400, allow_inf_nan=False)]
JitterRatio = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/payments"
    )
    provider_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8081")
    provider_timeout_seconds: TimingSeconds = 10.0
    dispatch_poll_interval_seconds: TimingSeconds = 0.25
    dispatch_retry_base_delay_seconds: TimingSeconds = 0.25
    dispatch_retry_max_delay_seconds: TimingSeconds = 30.0
    dispatch_retry_jitter_ratio: JitterRatio = 0.2
    dispatch_claim_timeout_seconds: TimingSeconds = 30.0

    @model_validator(mode="after")
    def validate_dispatch_timing(self) -> Self:
        if self.dispatch_retry_base_delay_seconds > self.dispatch_retry_max_delay_seconds:
            raise ValueError("retry base delay cannot exceed maximum delay")
        if self.dispatch_claim_timeout_seconds <= self.provider_timeout_seconds:
            raise ValueError("claim timeout must exceed provider timeout")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
