"""Unit tests for the URL service (``app.services.url_service``)."""

from __future__ import annotations

import pytest
from app.config import Settings
from app.core.exceptions import (
    ConflictError,
    ExpiredError,
    NotFoundError,
    UrlValidationError,
)
from app.models.base import utcnow
from app.repositories.cache_repository import CacheRepository


class TestValidateUrl:
    async def test_empty(self, url_service):
        with pytest.raises(UrlValidationError):
            url_service.validate_url("")

    async def test_too_long(self, url_service):
        with pytest.raises(UrlValidationError):
            url_service.validate_url("https://e.com/" + "a" * 3000)

    async def test_bad_scheme(self, url_service):
        with pytest.raises(UrlValidationError):
            url_service.validate_url("ftp://e.com/x")
        with pytest.raises(UrlValidationError):
            url_service.validate_url("example.com/x")

    async def test_private_network(self, url_service):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
            with pytest.raises(UrlValidationError):
                url_service.validate_url(f"https://{host}/x")

    async def test_localhost_policy(self):
        from app.repositories.cache_repository import CacheRepository
        from app.repositories.url_repository import UrlRepository
        from app.services.url_service import UrlService

        settings = Settings.model_validate(
            {"environment": "test", "database_url": "sqlite://", "allow_localhost": False}
        )
        svc = UrlService(
            repo=UrlRepository(None),
            cache=CacheRepository(None, settings),
            settings=settings,
            next_sequence=_seq,
        )
        with pytest.raises(UrlValidationError):
            svc.validate_url("http://localhost/x")

    async def test_valid(self, url_service):
        url_service.validate_url("https://example.com/path?x=1")  # no raise


async def _seq() -> int:
    return 1


class TestCreate:
    async def test_new(self, url_service):
        url, created = await url_service.create(original_url="https://e.com/a")
        assert created is True
        assert url.original_url == "https://e.com/a"
        assert url.click_count == 0

    async def test_idempotent_reuse(self, url_service):
        first, _ = await url_service.create(original_url="https://e.com/i")
        second, created = await url_service.create(original_url="https://e.com/i")
        assert created is False
        assert second.short_code == first.short_code

    async def test_custom_alias(self, url_service):
        url, created = await url_service.create(
            original_url="https://e.com/a", custom_alias="my-link"
        )
        assert created is True
        assert url.short_code == "my-link"
        assert url.custom_alias is True

    async def test_custom_alias_invalid(self, url_service):
        with pytest.raises(UrlValidationError):
            await url_service.create(original_url="https://e.com/a", custom_alias="bad alias!")

    async def test_custom_alias_conflict(self, url_service):
        await url_service.create(original_url="https://e.com/one", custom_alias="dup")
        with pytest.raises(ConflictError):
            await url_service.create(original_url="https://e.com/two", custom_alias="dup")

    async def test_custom_alias_same_url_reuse(self, url_service):
        a, _ = await url_service.create(original_url="https://e.com/same", custom_alias="same")
        b, created = await url_service.create(
            original_url="https://e.com/same", custom_alias="same"
        )
        assert created is False
        assert b.short_code == a.short_code

    async def test_expires_in_sets_ttl(self, url_service):
        url, _ = await url_service.create(original_url="https://e.com/ttl", expires_in=3600)
        assert url.expires_at is not None
        assert url.expires_at > utcnow()


class TestResolve:
    async def test_hit_from_db_then_cache(self, url_service, cache_repo: CacheRepository):
        url, _ = await url_service.create(original_url="https://e.com/r1")
        # First resolve: cache miss -> DB, then write-through.
        res = await url_service.resolve(url.short_code)
        assert res.from_cache is False
        assert res.original_url == "https://e.com/r1"
        # Second resolve should be served from the cache.
        res2 = await url_service.resolve(url.short_code)
        assert res2.from_cache is True

    async def test_not_found(self, url_service, cache_repo: CacheRepository):
        with pytest.raises(NotFoundError):
            await url_service.resolve("nope")
        # Negative cache should now be primed.
        assert await cache_repo.is_known_missing("nope") is True

    async def test_expired(self, url_service, cache_repo: CacheRepository):
        url, _ = await url_service.create(original_url="https://e.com/exp")
        await url_service.soft_delete(url.short_code)
        with pytest.raises(ExpiredError):
            await url_service.resolve(url.short_code)


class TestLifecycle:
    async def test_soft_delete(self, url_service):
        url, _ = await url_service.create(original_url="https://e.com/d")
        assert await url_service.soft_delete(url.short_code) is True
        assert url.is_expired is True

    async def test_get_or_404(self, url_service):
        url, _ = await url_service.create(original_url="https://e.com/g")
        got = await url_service.get_or_404(url.short_code)
        assert got.short_code == url.short_code
        with pytest.raises(NotFoundError):
            await url_service.get_or_404("nope")
