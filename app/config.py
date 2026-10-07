"""Application configuration via pydantic-settings.

All settings are read from environment variables (or a local `.env` file).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logging import configure_logging


class Settings(BaseSettings):
    """Runtime configuration for shurly.

    Values can be provided via environment variables (prefixed with ``SHURLY_``)
    or a local ``.env`` file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SHURLY_",
        case_sensitive=False,
        extra="ignore",
    )

    # --- application ---
    app_name: str = "shurly"
    debug: bool = False
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    base_url: str = "http://localhost:8000"

    # --- database ---
    database_url: str = "postgresql+asyncpg://shurly:shurly@localhost:5432/shurly"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_echo: bool = False

    # --- redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl: int = Field(default=86_400, ge=60)

    # --- security / rate limiting ---
    api_keys_enabled: bool = True
    rate_limit_enabled: bool = True
    default_rate_limit: int = Field(default=100, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    # --- code generation strategy ---
    code_strategy: Literal["base62", "murmur3"] = "base62"
    idempotent: bool = True
    code_length: int = Field(default=7, ge=4, le=12)
    use_celery: bool = Field(
        default=True,
        description="Route analytics through Celery; False records synchronously.",
    )

    # --- misc ---
    allow_localhost: bool = Field(
        default=True,
        description="Allow http://localhost targets (disabled by default in production)",
    )
    max_url_length: int = Field(default=2048, ge=100)
    sentry_dsn: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    settings = Settings()
    configure_logging(settings)
    return settings
