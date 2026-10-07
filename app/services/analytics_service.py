"""Analytics service: records click events and exposes aggregated stats.

Clicks are captured *fire-and-forget*: the redirect handler enqueues the
event onto the Celery task queue (or, if the broker is down, spawns a
background asyncio task) and immediately returns the redirect. This keeps
the hot redirect path free of blocking analytics I/O, and a broken
analytics pipeline degrades rather than errors.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models import Analytics, Url
from app.repositories.cache_repository import CacheRepository
from app.repositories.url_repository import UrlRepository

log = get_logger(__name__)

# Strong references to in-flight fire-and-forget analytics tasks.
#
# ``asyncio.create_task`` returns a coroutine handle that the event loop
# keeps only *weakly*; if nothing else holds the task it can be garbage
# collected before it runs. Because the :class:`AnalyticsService` instance
# is rebuilt per request (see ``get_analytics_service``), storing the ref on
# the instance is not enough -- the instance is itself short-lived. A
# module-level set keeps each task alive until it completes, at which point
# a done-callback drops it so the set cannot grow without bound.
_PENDING_TASKS: set[asyncio.Task[None]] = set()


def _safe_referrer(value: str | None) -> str | None:
    """Truncate a referrer to a sane length (column is TEXT, keep rows small)."""
    if not value:
        return None
    return value[:2048]


def _safe_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    return value[:1024]


def _safe_ip(value: str | None) -> str | None:
    """Validate and normalize a client IP for the INET column."""
    if not value or value == "unknown":
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _detect_country(ip: str | None) -> str | None:
    """Best-effort GeoIP country detection (ISO 3166-1 alpha-2).

    Real GeoIP (MaxMind GeoLite2) is resolved by the background
    ``enrich_geoip`` task, where a database file can be loaded without
    penalizing redirect latency. Here we simply exclude private/reserved
    addresses (which have no country) and leave the rest to the rollup.
    """
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_private or addr.is_loopback or addr.is_unspecified or addr.is_reserved:
        return None
    return None  # public IP: country filled in asynchronously by enrich_geoip


class AnalyticsService:
    """Records clicks and computes statistics for short URLs."""

    def __init__(
        self,
        *,
        repo: UrlRepository,
        cache: CacheRepository,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._settings = settings

    def build_event(
        self,
        *,
        url: Url,
        referrer: str | None,
        user_agent: str | None,
        ip: str | None,
        country: str | None = None,
    ) -> Analytics:
        """Construct an :class:`Analytics` row from a raw request."""
        clean_ip = _safe_ip(ip)
        return Analytics(
            url_id=url.id,
            referrer=_safe_referrer(referrer),
            user_agent=_safe_user_agent(user_agent),
            ip_address=clean_ip,
            country=country or _detect_country(clean_ip),
        )

    async def record_click(
        self,
        *,
        short_code: str,
        referrer: str | None,
        user_agent: str | None,
        ip: str | None,
        country: str | None = None,
    ) -> Analytics | None:
        """Persist a click for *short_code* using the request-scoped repo.

        Never raises for analytics-only failures -- a broken analytics
        pipeline must not break redirects.
        """
        try:
            return await self._write_click(
                self._repo,
                short_code=short_code,
                referrer=referrer,
                user_agent=user_agent,
                ip=ip,
                country=country,
            )
        except SQLAlchemyError as exc:
            log.warning("analytics_write_failed", error=str(exc), code=short_code)
            return None
        except Exception as exc:
            log.warning("analytics_unexpected", error=str(exc), code=short_code)
            return None

    async def _write_click(
        self,
        repo: UrlRepository,
        *,
        short_code: str,
        referrer: str | None,
        user_agent: str | None,
        ip: str | None,
        country: str | None = None,
    ) -> Analytics | None:
        """Shared click-write core, parameterized on the repository."""
        url = await repo.get_by_code(short_code)
        if url is None:
            log.warning("click_for_missing_url", code=short_code)
            return None
        event = self.build_event(
            url=url,
            referrer=referrer,
            user_agent=user_agent,
            ip=ip,
            country=country,
        )
        await repo.add_analytics(event)
        await repo.increment_clicks(short_code)
        await self._cache.bump_clicks(short_code)
        return event

    async def stats(self, short_code: str) -> dict[str, Any]:
        """Return aggregated statistics for a short code."""
        try:
            return await self._repo.stats(short_code)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            log.error("stats_failed", error=str(exc), code=short_code)
            raise NotFoundError(f"short code {short_code!r} not found") from exc

    async def dispatch_click(
        self,
        *,
        short_code: str,
        referrer: str | None,
        user_agent: str | None,
        ip: str | None,
        use_celery: bool = True,
    ) -> str:
        """Fire-and-forget a click. Returns the delivery mode.

        Primary path is a Celery ``send_task`` -- a single Redis PUBLISH
        that returns in well under a millisecond, so the redirect never
        waits on analytics. If the broker is unavailable we fall back to a
        background ``asyncio`` task that opens its *own* session (the
        request-scoped one is already closed once the response is sent) so
        analytics are still recorded at-least-once.
        """
        if use_celery:
            try:
                from app.tasks.celery_app import celery_app

                celery_app.send_task(
                    "shurly.record_click",
                    kwargs={
                        "short_code": short_code,
                        "referrer": referrer,
                        "user_agent": user_agent,
                        "ip": ip,
                    },
                )
                return "celery"
            except Exception as exc:
                log.warning("celery_enqueue_failed", error=str(exc))

        task = asyncio.create_task(
            self._record_click_isolated(
                short_code=short_code,
                referrer=referrer,
                user_agent=user_agent,
                ip=ip,
            )
        )
        _PENDING_TASKS.add(task)
        task.add_done_callback(_PENDING_TASKS.discard)
        return "sync"

    async def _record_click_isolated(
        self,
        *,
        short_code: str,
        referrer: str | None,
        user_agent: str | None,
        ip: str | None,
    ) -> None:
        """Background fallback: record a click on a fresh session.

        Runs after the response is sent, so it cannot reuse the
        request-scoped session. Failures are logged, never raised.
        """
        from app.db import get_session_factory

        session: AsyncSession = get_session_factory()()
        try:
            await self._write_click(
                UrlRepository(session),
                short_code=short_code,
                referrer=referrer,
                user_agent=user_agent,
                ip=ip,
            )
            await session.commit()
        except Exception as exc:
            log.warning("analytics_background_failed", error=str(exc), code=short_code)
            await session.rollback()
        finally:
            await session.close()
