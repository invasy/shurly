"""Pydantic schemas for shurly."""

from app.schemas.analytics import (
    ClickOut,
    ClicksByCountry,
    ReferrerStat,
    UrlStats,
)
from app.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyDelete,
    ApiKeyOut,
)
from app.schemas.health import ComponentHealth, HealthOut, ReadyOut
from app.schemas.url import (
    RedirectInfo,
    UrlCreate,
    UrlMeta,
    UrlOut,
    UrlUpdate,
)

__all__ = [
    "ApiKeyCreate",
    "ApiKeyCreated",
    "ApiKeyDelete",
    "ApiKeyOut",
    "ClickOut",
    "ClicksByCountry",
    "ComponentHealth",
    "HealthOut",
    "ReadyOut",
    "RedirectInfo",
    "ReferrerStat",
    "UrlCreate",
    "UrlMeta",
    "UrlOut",
    "UrlStats",
    "UrlUpdate",
]
