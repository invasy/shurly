"""API tests for the ``GET /{short_code}`` redirect hot path."""

from __future__ import annotations

import httpx
import pytest
from app.services.analytics_service import AnalyticsService


@pytest.fixture
def deterministic_analytics(monkeypatch):
    """Make analytics fire synchronously on the request session.

    The default ``dispatch_click`` with ``use_celery=False`` spawns an
    ``asyncio.create_task`` that runs *after* the response is sent and opens
    its own session. On the test's single-connection (StaticPool) SQLite that
    races the next request's session. Patching it to ``record_click`` records
    on the request-scoped session, which commits atomically with the response
    -- deterministic and assertable.
    """

    async def _sync_dispatch(self, *, short_code, referrer, user_agent, ip, use_celery=True):
        await self.record_click(
            short_code=short_code,
            referrer=referrer,
            user_agent=user_agent,
            ip=ip,
        )
        return "sync"

    monkeypatch.setattr(AnalyticsService, "dispatch_click", _sync_dispatch)
    yield


async def _create(client: httpx.AsyncClient, headers, **overrides) -> str:
    payload = {"url": "https://example.com/target"}
    payload.update(overrides)
    resp = await client.post("/api/v1/urls", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["short_code"]


class TestRedirect:
    async def test_307_default(self, client, auth_headers, deterministic_analytics):
        code = await _create(client, auth_headers, url="https://e.com/one")
        resp = await client.get(f"/{code}")
        assert resp.status_code == 307
        assert resp.headers["location"] == "https://e.com/one"

    async def test_301_opt_in(self, client, auth_headers, deterministic_analytics):
        code = await _create(client, auth_headers, url="https://e.com/two")
        resp = await client.get(f"/{code}", headers={"X-Stable": "true"})
        assert resp.status_code == 301
        assert resp.headers["location"] == "https://e.com/two"

    async def test_not_found(self, client, deterministic_analytics):
        resp = await client.get("/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    async def test_expired_is_410(self, client, auth_headers, deterministic_analytics):
        code = await _create(client, auth_headers, url="https://e.com/gone")
        # Soft-delete, then redirect -> 410.
        assert (
            await client.delete(f"/api/v1/urls/{code}", headers=auth_headers)
        ).status_code == 204
        resp = await client.get(f"/{code}")
        assert resp.status_code == 410
        assert resp.json()["error"] == "expired"

    async def test_click_recorded(self, client, auth_headers, deterministic_analytics):
        code = await _create(client, auth_headers, url="https://e.com/clicks")
        assert (await client.get(f"/{code}")).status_code == 307
        assert (await client.get(f"/{code}")).status_code == 307
        meta = (await client.get(f"/api/v1/urls/{code}")).json()
        assert meta["click_count"] == 2

    async def test_stats_reflect_clicks(self, client, auth_headers, deterministic_analytics):
        code = await _create(client, auth_headers, url="https://e.com/stats")
        await client.get(f"/{code}", headers={"Referer": "https://google.com"})
        stats = (await client.get(f"/api/v1/urls/{code}/stats")).json()
        assert stats["total_clicks"] == 1
        assert stats["top_referrers"] == [{"referrer": "https://google.com", "count": 1}]
