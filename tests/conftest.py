"""Shared pytest fixtures for the shurly test-suite.

Design goals
------------
* **Hermetic.** No real Postgres, Redis, or Celery broker is ever contacted.
  The database is a per-test file-backed SQLite instance (shared-cache,
  single pooled connection) and the cache is ``fakeredis`` (with ``lupa``
  for the rate-limiter Lua script).
* **Injectable.** The app is built with :func:`app.main.create_app` using a
  dedicated :class:`~app.config.Settings` instance and a ``dependency_overrides``
  on ``get_settings``. Infrastructure singletons (engine, redis cache, rate
  limiter) are swapped in through the ``app.db.set_engine`` and
  ``app.dependencies.set_cache_instance`` hooks, so the *real* dependency
  providers (``get_db_session``, ``get_cache``) work untouched.
* **Deterministic.** Analytics dispatch is monkeypatched in the API tests so
  click recording is synchronous and assertable.

Every fixture is function-scoped for full isolation between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import app.db as dbmod
import app.dependencies as deps
import fakeredis.aioredis
import httpx
import pytest
from app.cache import CircuitBreaker, RedisCache
from app.config import Settings
from app.main import create_app
from app.models import Base
from app.repositories.cache_repository import CacheRepository
from app.repositories.url_repository import UrlRepository
from app.services.analytics_service import AnalyticsService
from app.services.api_key_service import ApiKeyService
from app.services.url_service import UrlService
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool


# ``Settings`` is a pydantic BaseSettings; ``model_validate`` bypasses env.
def _test_settings(database_url: str) -> Settings:
    return Settings.model_validate(
        {
            "environment": "test",
            "log_level": "WARNING",
            "base_url": "http://testserver",
            "database_url": database_url,
            "redis_url": "redis://localhost:0/0",  # never connected in tests
            "api_keys_enabled": True,
            "rate_limit_enabled": True,
            "default_rate_limit": 100,
            "rate_limit_window_seconds": 60,
            "code_strategy": "base62",
            "code_length": 7,
            "idempotent": True,
            "use_celery": False,  # synchronous analytics path (deterministic)
            "allow_localhost": True,
        }
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[None]:
    """A per-test SQLite database wired into the process engine.

    A unique file per test (via ``tmp_path``) guarantees isolation; ``StaticPool``
    pins a single shared connection so the in-memory-ish file behaves like a
    single shared connection (no cross-process visibility surprises).
    """
    db_file = tmp_path / "shurly_test.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}?uri=true"
    engine = create_async_engine(
        url,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    dbmod.set_engine(engine)
    try:
        yield
    finally:
        await dbmod.dispose_engine()


@pytest.fixture
async def cache() -> AsyncIterator[RedisCache]:
    """A fakeredis-backed cache wired into the process cache + rate limiter."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_obj = RedisCache(client, CircuitBreaker(failure_threshold=5, cooldown_seconds=0.5))
    deps.set_cache_instance(cache_obj)
    try:
        yield cache_obj
    finally:
        deps.set_cache_instance(None)
        await client.aclose()


@pytest.fixture
async def settings(db: None, cache: RedisCache) -> Settings:
    """Test settings bound to the per-test database URL."""
    # Reconstruct from the engine so database_url matches what the app uses.
    engine_url = dbmod.get_engine().url.render_as_string(hide_password=True)
    return _test_settings(engine_url)


@pytest.fixture
async def app(settings: Settings, db: None, cache: RedisCache):
    """A fully-wired FastAPI app with the test settings injected."""
    from app.config import get_settings

    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx ASGI test client (does not follow redirects by default)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        yield c


# --- service / repository fixtures (for unit tests) ----------------------
@pytest.fixture
async def session(db: None) -> AsyncIterator[AsyncSession]:
    """A committed-on-success async session bound to the per-test engine."""
    factory = dbmod.get_session_factory()
    s = factory()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
async def url_repo(session: AsyncSession) -> UrlRepository:
    return UrlRepository(session)


@pytest.fixture
async def cache_repo(cache: RedisCache, settings: Settings) -> CacheRepository:
    return CacheRepository(cache, settings)


@pytest.fixture
async def url_service(
    session: AsyncSession,
    cache_repo: CacheRepository,
    settings: Settings,
    url_repo: UrlRepository,
) -> UrlService:
    seq = {"n": 0}

    async def _next() -> int:
        seq["n"] += 1
        return seq["n"]

    return UrlService(
        repo=url_repo,
        cache=cache_repo,
        settings=settings,
        next_sequence=_next,
    )


@pytest.fixture
async def analytics_service(
    session: AsyncSession,
    cache_repo: CacheRepository,
    settings: Settings,
    url_repo: UrlRepository,
) -> AnalyticsService:
    return AnalyticsService(repo=url_repo, cache=cache_repo, settings=settings)


@pytest.fixture
async def api_key_service(session: AsyncSession, settings: Settings) -> ApiKeyService:
    return ApiKeyService(session, settings)


# --- helpers shared by tests ---------------------------------------------
@pytest.fixture
async def api_key(client: httpx.AsyncClient, db: None) -> str:
    """Create a fresh API key and return its one-time plaintext."""
    resp = await client.post("/api/v1/keys", json={"description": "test key", "rate_limit": 100})
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


@pytest.fixture
async def auth_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


__all__ = [
    "api_key",
    "app",
    "auth_headers",
    "cache",
    "client",
    "db",
    "settings",
]
