"""Pydantic models for API key management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    """Request body to generate a new API key."""

    description: str | None = Field(
        default=None, max_length=255, description="Human-readable label."
    )
    rate_limit: int = Field(
        default=100, ge=1, le=10_000, description="Requests allowed per window."
    )


class ApiKeyOut(BaseModel):
    """An API key record (without the plaintext)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str | None
    rate_limit: int
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    """The one-time response containing the plaintext key."""

    key: str = Field(description="The plaintext API key. Shown only once.")


class ApiKeyDelete(BaseModel):
    """Confirmation of a deleted key."""

    id: uuid.UUID
    deleted: bool = True
