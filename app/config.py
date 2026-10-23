from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://generation:generation@localhost:5432/generation"
    webhook_secret: str = "local-webhook-secret-change-me"
    admin_key: str = "local-admin-key-change-me"
    public_base_url: str = "http://localhost:8000"
    alpha_url: str = "http://localhost:8001"
    beta_url: str = "http://localhost:8002"
    provider_name: Literal["alpha", "beta"] = "alpha"
    provider_timeout: float = Field(default=60, gt=0)
    http_timeout: float = Field(default=5, gt=0)
    lease_seconds: float = Field(default=15, gt=0)
    poll_interval: float = Field(default=0.25, gt=0)
    pool_min: int = Field(default=1, ge=1)
    pool_max: int = Field(default=8, ge=1)
    demo_enabled: bool = False
    run_background: bool = True

    @model_validator(mode="after")
    def check_settings(self):
        if self.lease_seconds <= self.http_timeout:
            raise ValueError("LEASE_SECONDS must exceed HTTP_TIMEOUT")
        if self.pool_max < self.pool_min:
            raise ValueError("POOL_MAX must be >= POOL_MIN")
        return self

    def provider_url(self, name: str) -> str:
        return {"alpha": self.alpha_url, "beta": self.beta_url}[name].rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
