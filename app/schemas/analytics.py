"""Pydantic models for analytics / statistics."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ClickOut(BaseModel):
    """A single click event."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    clicked_at: datetime
    referrer: str | None
    user_agent: str | None
    ip_address: str | None
    country: str | None


class ClicksByCountry(BaseModel):
    """Aggregated clicks per country (ISO 3166-1 alpha-2)."""

    country: str
    count: int


class ReferrerStat(BaseModel):
    """Top referrers."""

    referrer: str
    count: int


class UrlStats(BaseModel):
    """Aggregated statistics for a short URL."""

    short_code: str
    total_clicks: int
    clicks_last_24h: int
    clicks_last_7d: int
    clicks_last_30d: int
    countries: list[ClicksByCountry] = Field(default_factory=list)
    top_referrers: list[ReferrerStat] = Field(default_factory=list)
    first_click_at: datetime | None = None
    last_click_at: datetime | None = None
