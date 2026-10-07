"""Unit tests for the Redis cache-aside repository (``app.repositories.cache_repository``)."""

from __future__ import annotations

import pytest
from app.cache import RedisCache
from app.config import Settings
from app.repositories.cache_repository import CacheRepository, UrlCacheRecord


@pytest.fixture
def repo(cache: RedisCache) -> CacheRepository:
    settings = Settings.model_validate(
        {"environment": "test", "redis_cache_ttl": 60, "database_url": "sqlite://"}
    )
    return CacheRepository(cache, settings)


class TestGetSet:
    async def test_miss(self, repo):
        assert await repo.get_url("abc") is None

    async def test_roundtrip(self, repo):
        await repo.set_url(UrlCacheRecord(short_code="abc", original_url="https://e.com/x"))
        rec = await repo.get_url("abc")
        assert rec is not None
        assert rec.short_code == "abc"
        assert rec.original_url == "https://e.com/x"

    async def test_set_uses_ttl(self, repo, cache):
        await repo.set_url(UrlCacheRecord(short_code="abc", original_url="https://e.com"))
        ttl = await cache.client.ttl("shurly:url:abc")
        assert 0 < ttl <= 60

    async def test_malformed_value_returns_none(self, repo, cache):
        await cache.set("shurly:url:bad", "no-tab-separator")
        assert await repo.get_url("bad") is None


class TestInvalidate:
    async def test_invalidate_purges(self, repo, cache):
        await repo.set_url(UrlCacheRecord(short_code="abc", original_url="https://e.com"))
        await repo.mark_missing("abc")
        await cache.incr("shurly:clicks:abc")
        await repo.invalidate("abc")
        assert await repo.get_url("abc") is None
        assert await repo.is_known_missing("abc") is False
        assert await cache.client.get("shurly:clicks:abc") is None


class TestNegative:
    async def test_mark_and_check(self, repo):
        assert await repo.is_known_missing("x") is False
        await repo.mark_missing("x")
        assert await repo.is_known_missing("x") is True


class TestClicks:
    async def test_bump(self, repo):
        assert await repo.bump_clicks("a") == 1
        assert await repo.bump_clicks("a") == 2


class TestFailOpen:
    async def test_get_url_fail_open(self, repo):
        repo._cache.breaker.record_failure()
        repo._cache.breaker.record_failure()
        repo._cache.breaker.record_failure()
        repo._cache.breaker.record_failure()
        repo._cache.breaker.record_failure()
        assert await repo.get_url("abc") is None
        assert await repo.is_known_missing("abc") is False
        assert await repo.bump_clicks("abc") == 0

    async def test_set_url_fail_open(self, repo):
        for _ in range(5):
            repo._cache.breaker.record_failure()
        # Must not raise even though the breaker is open.
        await repo.set_url(UrlCacheRecord(short_code="abc", original_url="https://e.com"))
        await repo.invalidate("abc")
        await repo.mark_missing("abc")
