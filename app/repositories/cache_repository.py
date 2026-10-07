"""Redis-backed cache repository implementing the cache-aside pattern."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.cache import CircuitBreakerError, RedisCache
from app.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

_URL_KEY = "shurly:url:{code}"
_NEGATIVE_KEY = "shurly:neg:{code}"
_NEGATIVE_TTL = 30  # seconds
_CLICK_KEY = "shurly:clicks:{code}"


@dataclass(slots=True)
class UrlCacheRecord:
    """Cached form of a Url: just enough to serve a redirect."""

    short_code: str
    original_url: str
    expires_at: datetime | None = None


class CacheRepository:
    """Redis layer for URL redirects.

    Cache-aside: :meth:`get_url` returns a hit or ``None``; on ``None`` the
    caller loads from the DB and calls :meth:`set_url`. Negative results are
    cached briefly to stop hammering the DB for typo'd short codes (this
    doubles as a poor-man's bloom filter for existence pre-checks).

    Every method fails *open*: a Redis outage is logged and the caller
    falls through to the database, so the redirect path degrades gracefully
    rather than erroring.
    """

    def __init__(self, cache: RedisCache, settings: Settings) -> None:
        self._cache = cache
        self._settings = settings

    # --- lookup ---
    async def get_url(self, short_code: str) -> UrlCacheRecord | None:
        """Return the cached record for *short_code*, or ``None`` on miss/failure."""
        try:
            raw = await self._cache.get(_URL_KEY.format(code=short_code))
        except CircuitBreakerError as exc:
            log.warning("cache_read_failed", error=str(exc), code=short_code)
            return None
        if raw is None:
            return None
        code, sep, original = raw.partition("\t")
        if not sep:
            return None
        return UrlCacheRecord(short_code=code, original_url=original)

    # --- write ---
    async def set_url(self, record: UrlCacheRecord) -> None:
        """Cache a URL record. Fails open (logs, does not raise)."""
        key = _URL_KEY.format(code=record.short_code)
        value = f"{record.short_code}\t{record.original_url}"
        try:
            await self._cache.set(key, value, ex=self._settings.redis_cache_ttl)
        except CircuitBreakerError as exc:
            log.warning("cache_write_failed", error=str(exc), code=record.short_code)

    async def invalidate(self, short_code: str) -> None:
        """Purge a short code from the cache (deletion / expiration)."""
        try:
            await self._cache.delete(
                _URL_KEY.format(code=short_code),
                _NEGATIVE_KEY.format(code=short_code),
                _CLICK_KEY.format(code=short_code),
            )
        except CircuitBreakerError as exc:
            log.warning("cache_invalidate_failed", error=str(exc), code=short_code)

    # --- negative cache (poor-man's bloom filter) ---
    async def mark_missing(self, short_code: str) -> None:
        """Record that *short_code* was not found (negative cache)."""
        try:
            await self._cache.set(_NEGATIVE_KEY.format(code=short_code), "1", ex=_NEGATIVE_TTL)
        except CircuitBreakerError as exc:
            log.warning("cache_negative_failed", error=str(exc), code=short_code)

    async def is_known_missing(self, short_code: str) -> bool:
        """Quick existence pre-check before hitting the DB."""
        try:
            return await self._cache.exists(_NEGATIVE_KEY.format(code=short_code))
        except CircuitBreakerError as exc:
            log.warning("cache_negative_read_failed", error=str(exc), code=short_code)
            return False

    # --- counters ---
    async def bump_clicks(self, short_code: str) -> int:
        """Best-effort Redis click counter for fast stats.

        The authoritative count lives in ``urls.click_count``; this Redis
        counter is a hot, low-latency approximation that the aggregation
        task reconciles with the DB.
        """
        try:
            return await self._cache.incr(_CLICK_KEY.format(code=short_code), ex=86_400)
        except CircuitBreakerError as exc:
            log.warning("cache_click_bump_failed", error=str(exc), code=short_code)
            return 0
