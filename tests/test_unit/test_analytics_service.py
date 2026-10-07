"""Unit tests for the analytics service (``app.services.analytics_service``)."""

from __future__ import annotations

import asyncio

from app.models import Analytics
from app.services.analytics_service import (
    AnalyticsService,
    _detect_country,
    _safe_ip,
    _safe_referrer,
    _safe_user_agent,
)


class TestHelpers:
    def test_safe_referrer(self):
        assert _safe_referrer(None) is None
        assert _safe_referrer("") is None
        assert _safe_referrer("x" * 3000) == "x" * 2048
        assert _safe_referrer("google") == "google"

    def test_safe_user_agent(self):
        assert _safe_user_agent(None) is None
        assert _safe_user_agent("x" * 2000) == "x" * 1024

    def test_safe_ip(self):
        assert _safe_ip(None) is None
        assert _safe_ip("unknown") is None
        assert _safe_ip("8.8.8.8") == "8.8.8.8"
        assert _safe_ip("::1") == "::1"
        assert _safe_ip("not-an-ip") is None

    def test_detect_country(self):
        # Private/loopback/reserved have no country; public also deferred.
        assert _detect_country(None) is None
        assert _detect_country("127.0.0.1") is None
        assert _detect_country("10.0.0.1") is None
        assert _detect_country("8.8.8.8") is None


class TestBuildEvent:
    async def test_build_event_cleans(self, analytics_service, url_service):
        url, _ = await url_service.create(original_url="https://e.com/b")
        ua = "a" * 2000
        ev = analytics_service.build_event(
            url=url,
            referrer="r" * 3000,
            user_agent=ua,
            ip="bad-ip",
        )
        assert ev.url_id == url.id
        assert ev.referrer == "r" * 2048
        assert ev.user_agent == ua[:1024]
        assert ev.ip_address is None

    async def test_build_event_valid_ip(self, analytics_service, url_service):
        url, _ = await url_service.create(original_url="https://e.com/c")
        ev = analytics_service.build_event(url=url, referrer=None, user_agent=None, ip="1.2.3.4")
        assert ev.ip_address == "1.2.3.4"
        assert ev.country is None


class TestRecordClick:
    async def test_record_click(self, analytics_service, url_service, cache_repo):
        url, _ = await url_service.create(original_url="https://e.com/r")
        ev = await analytics_service.record_click(
            short_code=url.short_code,
            referrer="ref",
            user_agent="ua",
            ip="1.2.3.4",
        )
        assert ev is not None
        assert url.click_count == 1
        # The event was persisted.
        from sqlalchemy import select

        rows = (
            (
                await analytics_service._repo._s.execute(
                    select(Analytics).where(Analytics.url_id == url.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def test_record_click_missing_url(self, analytics_service):
        assert (
            await analytics_service.record_click(
                short_code="nope",
                referrer=None,
                user_agent=None,
                ip=None,
            )
            is None
        )


class TestDispatch:
    async def test_celery_path(self, analytics_service, monkeypatch):
        from app.tasks.celery_app import celery_app as _celery

        calls = []

        def fake_send_task(name, *, kwargs):
            calls.append((name, kwargs))

        monkeypatch.setattr(_celery, "send_task", fake_send_task)

        mode = await analytics_service.dispatch_click(
            short_code="abc", referrer="r", user_agent="u", ip="1.2.3.4", use_celery=True
        )
        assert mode == "celery"
        assert calls == [
            (
                "shurly.record_click",
                {"short_code": "abc", "referrer": "r", "user_agent": "u", "ip": "1.2.3.4"},
            )
        ]

    async def test_celery_fallback_on_broker_error(self, analytics_service, monkeypatch):
        from app.tasks.celery_app import celery_app as _celery

        def broken_send_task(name, *, kwargs):
            raise RuntimeError("broker down")

        monkeypatch.setattr(_celery, "send_task", broken_send_task)
        seen = {}

        async def fake_isolated(self, **kw):
            seen.update(kw)

        monkeypatch.setattr(AnalyticsService, "_record_click_isolated", fake_isolated)

        mode = await analytics_service.dispatch_click(
            short_code="abc", referrer="r", user_agent="u", ip="1.2.3.4", use_celery=True
        )
        assert mode == "sync"
        await asyncio.sleep(0)  # let the scheduled task run
        assert seen["short_code"] == "abc"

    async def test_sync_path(self, analytics_service, monkeypatch):
        seen = {}

        async def fake_isolated(self, **kw):
            seen.update(kw)

        monkeypatch.setattr(AnalyticsService, "_record_click_isolated", fake_isolated)
        mode = await analytics_service.dispatch_click(
            short_code="xyz", referrer=None, user_agent=None, ip=None, use_celery=False
        )
        assert mode == "sync"
        await asyncio.sleep(0)
        assert seen["short_code"] == "xyz"
