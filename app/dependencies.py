"""FastAPI dependency providers.

Centralizes construction of services, repositories, the Redis client, and
auth/rate-limit middleware so that routes stay declarative and every
component is injectable in tests (via ``app.dependency_overrides``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from itertools import count as _count

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import RedisCache
from app.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import SlidingWindowRateLimiter
from app.db import get_session_factory
from app.models import ApiKey
from app.repositories.cache_repository import CacheRepository
from app.repositories.url_repository import UrlRepository
from app.services.analytics_service import AnalyticsService
from app.services.api_key_service import ApiKeyService
from app.services.url_service import UrlService

# Process-wide singletons (lazy).
_redis_cache: RedisCache | None = None
_rate_limiter: SlidingWindowRateLimiter | None = None
# Fallback monotonic counter used only on non-Postgres dialects (tests).
_seq_counter = _count(1)


# --- infrastructure ---
def get_cache(settings: Settings = Depends(get_settings)) -> RedisCache:
    """Return (and lazily create) the shared Redis cache client."""
    global _redis_cache
    if _redis_cache is None:
        _redis_cache = RedisCache.create(settings)
    return _redis_cache


def set_cache_instance(cache: RedisCache | None) -> None:
    """Replace (or clear) the shared Redis cache -- used by tests."""
    global _redis_cache, _rate_limiter
    _redis_cache = cache
    if cache is not None:
        _rate_limiter = SlidingWindowRateLimiter(cache.client)


def get_rate_limiter(
    settings: Settings = Depends(get_settings),
    cache: RedisCache = Depends(get_cache),
) -> SlidingWindowRateLimiter:
    """Return (and lazily create) the sliding-window rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = SlidingWindowRateLimiter(cache.client)
    return _rate_limiter


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a transaction-scoped async session (commit/rollback handled)."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# --- sequences ---
async def next_sequence(session: AsyncSession) -> int:
    """Draw the next value for the Base62 strategy.

    On PostgreSQL this consumes the dedicated ``urls.id`` sequence, giving a
    strictly monotonic, process-wide-unique source (codes unique by
    construction). On other dialects (in-memory SQLite in tests) there is no
    such sequence, so a process-local counter is used; collision safety is
    still guaranteed by the unique ``short_code`` index.
    """
    if session.bind.dialect.name == "postgresql":
        result = await session.execute(select(func.nextval("urls_id_seq")))
        return int(result.scalar_one())
    return next(_seq_counter)


# --- repositories & services ---
def get_url_repository(session: AsyncSession = Depends(get_db_session)) -> UrlRepository:
    return UrlRepository(session)


def get_cache_repository(
    settings: Settings = Depends(get_settings),
    cache: RedisCache = Depends(get_cache),
) -> CacheRepository:
    return CacheRepository(cache, settings)


def get_url_service(
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    cache: RedisCache = Depends(get_cache),
) -> UrlService:
    return UrlService(
        repo=UrlRepository(session),
        cache=CacheRepository(cache, settings),
        settings=settings,
        next_sequence=lambda: next_sequence(session),
    )


def get_analytics_service(
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    cache: RedisCache = Depends(get_cache),
) -> AnalyticsService:
    return AnalyticsService(
        repo=UrlRepository(session),
        cache=CacheRepository(cache, settings),
        settings=settings,
    )


def get_api_key_service(
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ApiKeyService:
    return ApiKeyService(session, settings)


# --- authentication & rate limiting ---
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ApiKey:
    """Mandatory API-key auth for write endpoints.

    Stores the authenticated :class:`ApiKey` on ``request.state`` so the
    rate limiter and the URL service can both see the caller's identity.
    """
    settings: Settings = request.app.state.settings
    header_value: str | None = await _api_key_header(request)
    if not settings.api_keys_enabled:
        request.state.api_key = None
        request.state.user_id = None
        return _anonymous_key()
    if not header_value:
        raise AuthenticationError("missing API key (X-API-Key header)")
    svc = ApiKeyService(session, settings)
    record = await svc.verify(header_value)
    request.state.api_key = record
    request.state.user_id = record.id
    return record


async def optional_api_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ApiKey | None:
    """Best-effort API-key auth: anonymous callers are allowed."""
    settings: Settings = request.app.state.settings
    header_value: str | None = await _api_key_header(request)
    if not header_value or not settings.api_keys_enabled:
        request.state.api_key = None
        request.state.user_id = None
        return None
    try:
        svc = ApiKeyService(session, settings)
        record = await svc.verify(header_value)
    except AuthenticationError:
        record = None
    request.state.api_key = record
    request.state.user_id = record.id if record else None
    return record


def _anonymous_key() -> ApiKey:
    """Synthetic key for auth-disabled mode (never persisted)."""
    return ApiKey(
        id=uuid.UUID(int=0),
        key_hash="anonymous:0",
        rate_limit=1_000_000,
    )


async def enforce_rate_limit(
    request: Request,
    settings: Settings = Depends(get_settings),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> None:
    """Sliding-window rate limit keyed by API key (or IP fallback).

    Runs *after* authentication: ``require_api_key`` populates
    ``request.state``. Disabled when ``settings.rate_limit_enabled`` is
    False. A limiter failure fails open (it logs and lets the request
    through) -- rate limiting must never take the service down.
    """
    if not settings.rate_limit_enabled:
        return
    api_key: ApiKey | None = getattr(request.state, "api_key", None)
    if api_key is not None and api_key.id != uuid.UUID(int=0):
        scope = f"key:{api_key.id}"
        limit = api_key.rate_limit
    else:
        from app.core.security import ip_from_request

        scope = f"ip:{ip_from_request(request)}"
        limit = settings.default_rate_limit
    await limiter.check(scope, limit, settings.rate_limit_window_seconds)
