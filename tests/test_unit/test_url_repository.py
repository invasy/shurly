"""Unit tests for the URL repository (``app.repositories.url_repository``)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from app.core.exceptions import ConflictError, NotFoundError
from app.models import Analytics, Url
from app.models.base import utcnow
from app.repositories.url_repository import UrlRepository
from sqlalchemy.ext.asyncio import AsyncSession


async def _make(session: AsyncSession, **kw) -> Url:
    defaults = {"short_code": "abc123", "original_url": "https://example.com/x"}
    defaults.update(kw)
    u = Url(**defaults)
    session.add(u)
    await session.flush()
    return u


class TestReads:
    async def test_get_by_code_miss(self, url_repo: UrlRepository):
        assert await url_repo.get_by_code("nope") is None

    async def test_get_by_code_hit(self, url_repo: UrlRepository):
        await _make(url_repo._s, short_code="abc")
        u = await url_repo.get_by_code("abc")
        assert u is not None and u.original_url == "https://example.com/x"

    async def test_find_by_url_anonymous(self, url_repo: UrlRepository):
        await _make(url_repo._s, short_code="a", original_url="https://e.com/1")
        found = await url_repo.find_by_url("https://e.com/1", user_id=None)
        assert found is not None and found.short_code == "a"
        assert await url_repo.find_by_url("https://missing", user_id=None) is None

    async def test_find_by_url_scoped_to_user(self, url_repo: UrlRepository):
        uid = uuid.uuid4()
        await _make(url_repo._s, short_code="a", original_url="https://e.com/1", user_id=uid)
        # A different owner cannot see the row.
        assert await url_repo.find_by_url("https://e.com/1", user_id=uuid.uuid4()) is None
        assert await url_repo.find_by_url("https://e.com/1", user_id=uid) is not None

    async def test_find_by_url_skips_expired(self, url_repo: UrlRepository):
        past = utcnow() - timedelta(seconds=1)
        await _make(
            url_repo._s,
            short_code="a",
            original_url="https://e.com/1",
            expires_at=past,
        )
        assert await url_repo.find_by_url("https://e.com/1", user_id=None) is None


class TestCreateOrGet:
    async def test_new(self, url_repo: UrlRepository):
        u, created = await url_repo.create_or_get(
            original_url="https://e.com/new", short_code="new1"
        )
        assert created is True and u.short_code == "new1"

    async def test_idempotent_returns_existing(self, url_repo: UrlRepository):
        await _make(url_repo._s, short_code="dup", original_url="https://e.com/dup")
        u, created = await url_repo.create_or_get(
            original_url="https://e.com/dup", short_code="other", idempotent=True
        )
        assert created is False and u.short_code == "dup"

    async def test_non_idempotent_conflict(self, url_repo: UrlRepository):
        await _make(url_repo._s, short_code="taken", original_url="https://e.com/a")
        with pytest.raises(ConflictError):
            await url_repo.create_or_get(
                original_url="https://e.com/b", short_code="taken", idempotent=False
            )

    async def test_sets_expires_at(self, url_repo: UrlRepository):
        u, _ = await url_repo.create_or_get(
            original_url="https://e.com/ttl", short_code="ttl1", expires_in=3600
        )
        assert u.expires_at is not None
        assert u.expires_at > utcnow()


class TestStatsAndClicks:
    async def test_increment_clicks(self, url_repo: UrlRepository):
        await _make(url_repo._s, short_code="c1")
        assert await url_repo.increment_clicks("c1") == 1
        assert await url_repo.increment_clicks("c1") == 2
        assert await url_repo.increment_clicks("missing") == 0

    async def test_add_analytics(self, url_repo: UrlRepository):
        u = await _make(url_repo._s, short_code="a1")
        ev = await url_repo.add_analytics(Analytics(url_id=u.id, referrer="r", country="US"))
        assert ev.id is not None

    async def test_stats_aggregates(self, url_repo: UrlRepository):
        u = await _make(url_repo._s, short_code="s1")
        now = utcnow()
        rows = [
            (now - timedelta(hours=1), "US", "google"),
            (now - timedelta(days=3), "US", "google"),
            (now - timedelta(days=10), "DE", "twitter"),
            (now - timedelta(days=45), None, None),
        ]
        for t, country, ref in rows:
            ev = Analytics(url_id=u.id, clicked_at=t, country=country, referrer=ref)
            url_repo._s.add(ev)
        await url_repo._s.flush()

        stats = await url_repo.stats("s1")
        assert stats["total_clicks"] == 4
        assert stats["clicks_last_24h"] == 1
        assert stats["clicks_last_7d"] == 2
        assert stats["clicks_last_30d"] == 3
        by_country = {c["country"]: c["count"] for c in stats["countries"]}
        assert by_country == {"US": 2, "DE": 1}
        by_ref = {r["referrer"]: r["count"] for r in stats["top_referrers"]}
        assert by_ref == {"google": 2, "twitter": 1}

    async def test_stats_missing_raises(self, url_repo: UrlRepository):
        with pytest.raises(NotFoundError):
            await url_repo.stats("nope")

    async def test_soft_delete(self, url_repo: UrlRepository):
        await _make(url_repo._s, short_code="d1")
        assert await url_repo.soft_delete("d1") is True
        # Re-fetch: soft-delete sets expires_at in the past -> expired.
        u = await url_repo.get_by_code("d1")
        assert u is not None
        assert u.is_expired is True
        # Re-deleting an already-expired row still matches (rowcount > 0).
        assert await url_repo.soft_delete("d1") is True

    async def test_soft_delete_missing(self, url_repo: UrlRepository):
        assert await url_repo.soft_delete("nope") is False
