"""``GET /{short_code}`` -- the redirect hot path.

This is the latency-critical endpoint. It:
  1. resolves the short code (Redis first, DB fallback) -- sub-millisecond
     on a cache hit,
  2. fires the click off (Celery PUBLISH or a background asyncio task)
     without awaiting it, and
  3. returns a redirect whose ``Location`` header points at the original
     URL.

The default status is ``307 Temporary Redirect`` (per the spec) so any
request method / body is preserved by redirecting clients. A client can
opt into ``301 Permanent Redirect`` with the ``X-Stable: true`` header,
which is friendlier for long-lived links and aggressive HTTP caching.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.middleware import REDIRECTS_TOTAL
from app.core.security import ip_from_request
from app.dependencies import get_analytics_service, get_url_service
from app.services import AnalyticsService, UrlService

log = get_logger(__name__)

router = APIRouter(tags=["redirect"])


def _is_stable(request: Request) -> bool:
    """301 only when the client opts in via ``X-Stable: true``."""
    return request.headers.get("x-stable", "").lower() == "true"


@router.get(
    "/{short_code}",
    summary="Redirect to the original URL",
    responses={
        307: {"description": "Temporary redirect to the original URL"},
        301: {"description": "Permanent redirect (opt-in via X-Stable header)"},
        404: {"description": "Short code not found"},
        410: {"description": "Short code expired"},
    },
)
async def redirect(
    short_code: str,
    request: Request,
    url_service: UrlService = Depends(get_url_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Redirect a short code to its original URL and log the click."""
    resolved = await url_service.resolve(short_code)
    REDIRECTS_TOTAL.inc()

    # Fire-and-forget analytics; never awaited beyond the PUBLISH/enqueue.
    await analytics.dispatch_click(
        short_code=short_code,
        referrer=request.headers.get("referer"),
        user_agent=request.headers.get("user-agent"),
        ip=ip_from_request(request),
        use_celery=settings.use_celery,
    )

    status_code = (
        status.HTTP_301_MOVED_PERMANENTLY
        if _is_stable(request)
        else status.HTTP_307_TEMPORARY_REDIRECT
    )
    return RedirectResponse(
        url=resolved.original_url,
        status_code=status_code,
        headers={"X-Correlation-Id": getattr(request.state, "correlation_id", "")},
    )
