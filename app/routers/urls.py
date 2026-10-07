"""``/api/v1/urls`` -- CRUD for short URLs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.config import Settings, get_settings
from app.dependencies import (
    enforce_rate_limit,
    get_analytics_service,
    get_url_service,
    optional_api_key,
    require_api_key,
)
from app.models import Url
from app.schemas import (
    UrlCreate,
    UrlMeta,
    UrlOut,
    UrlStats,
)
from app.services import AnalyticsService, UrlService

router = APIRouter(prefix="/api/v1/urls", tags=["urls"])


def _to_out(url: Url, base_url: str) -> UrlOut:
    return UrlOut(
        short_code=url.short_code,
        short_url=f"{base_url}/{url.short_code}",
        original_url=url.original_url,
        created_at=url.created_at,
        expires_at=url.expires_at,
        click_count=url.click_count,
    )


@router.post(
    "",
    response_model=UrlOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a short URL",
    dependencies=[Depends(enforce_rate_limit)],
)
async def create_url(
    payload: UrlCreate,
    request: Request,
    service: UrlService = Depends(get_url_service),
    settings: Settings = Depends(get_settings),
    _auth: object = Depends(require_api_key),
) -> UrlOut:
    """Create a short URL.

    Requires the ``X-API-Key`` header. If ``custom_alias`` is supplied the
    code is used verbatim; otherwise a code is allocated per the configured
    strategy (Base62 sequence or Murmur3 hash). Idempotent by default: the
    same (url, user) returns the same code.
    """
    user_id: uuid.UUID | None = getattr(request.state, "user_id", None)
    url, _created = await service.create(
        original_url=payload.url,
        custom_alias=payload.custom_alias,
        expires_in=payload.expires_in,
        idempotent=payload.idempotent,
        user_id=user_id,
    )
    return _to_out(url, settings.base_url)


@router.get(
    "/{short_code}",
    response_model=UrlMeta,
    summary="Get URL metadata",
)
async def get_url(
    short_code: str,
    service: UrlService = Depends(get_url_service),
    settings: Settings = Depends(get_settings),
    _auth: object = Depends(optional_api_key),
) -> UrlMeta:
    """Return metadata for a short code (original URL, counts, TTL)."""
    url = await service.get_or_404(short_code)
    return UrlMeta(
        id=url.id,
        short_code=url.short_code,
        short_url=f"{settings.base_url}/{url.short_code}",
        original_url=url.original_url,
        custom_alias=url.custom_alias,
        created_at=url.created_at,
        expires_at=url.expires_at,
        click_count=url.click_count,
    )


@router.delete(
    "/{short_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete (soft-delete) a short URL",
    dependencies=[Depends(enforce_rate_limit)],
)
async def delete_url(
    short_code: str,
    request: Request,
    service: UrlService = Depends(get_url_service),
    _auth: object = Depends(require_api_key),
) -> None:
    """Soft-delete a short URL (expires it; history preserved)."""
    deleted = await service.soft_delete(short_code)
    if not deleted:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"short code {short_code!r} not found")


@router.get(
    "/{short_code}/stats",
    response_model=UrlStats,
    summary="Get analytics for a short URL",
)
async def get_url_stats(
    short_code: str,
    service: AnalyticsService = Depends(get_analytics_service),
    _auth: object = Depends(optional_api_key),
) -> UrlStats:
    """Aggregated click statistics (totals, windows, top countries/referrers)."""
    data = await service.stats(short_code)
    return UrlStats(**data)
