"""Pydantic request/response models for the URL and redirect APIs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UrlCreate(BaseModel):
    """Payload for ``POST /api/v1/urls``."""

    url: str = Field(
        ...,
        description="The long URL to shorten (must be an absolute http/https URL).",
        min_length=1,
        max_length=2048,
    )
    custom_alias: str | None = Field(
        default=None,
        description="Optional custom short code (e.g. 'my-link').",
        min_length=1,
        max_length=64,
    )
    expires_in: int | None = Field(
        default=None,
        ge=1,
        description="Optional TTL in seconds. The short code expires after this.",
    )
    idempotent: bool | None = Field(
        default=None,
        description=(
            "If True (or omitted and global default is on) identical "
            "(url, user) pairs return the same short code."
        ),
    )

    @field_validator("url")
    @classmethod
    def _validate_scheme(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url must not be blank")
        low = v.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("custom_alias")
    @classmethod
    def _validate_alias(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import re

        v = v.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", v):
            raise ValueError(
                "custom_alias must start with a letter/digit and contain "
                "only alphanumerics, '_' or '-'"
            )
        return v


class UrlOut(BaseModel):
    """Response for a created or fetched URL."""

    model_config = ConfigDict(from_attributes=True)

    short_code: str
    short_url: str
    original_url: str
    created_at: datetime
    expires_at: datetime | None
    click_count: int


class UrlUpdate(BaseModel):
    """Payload for updating TTL / alias (partial)."""

    expires_in: int | None = Field(default=None, ge=1)


class UrlMeta(BaseModel):
    """Metadata variant for ``GET /api/v1/urls/{short_code}``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    short_code: str
    short_url: str
    original_url: str
    custom_alias: bool
    created_at: datetime
    expires_at: datetime | None
    click_count: int


class RedirectInfo(BaseModel):
    """Body returned when a redirect is requested as JSON (e.g. by APIs)."""

    short_code: str
    original_url: str
    expires_at: datetime | None
    status: int
