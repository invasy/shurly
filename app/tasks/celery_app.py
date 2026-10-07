"""Celery application and background tasks for shurly.

Analytics are the primary background workload. The redirect path enqueues
a ``record_click`` task (fire-and-forget); the worker persists the event
and, on a schedule, runs rollups that (a) reconcile the Redis click
counters with ``urls.click_count`` and (b) enrich recent events with GeoIP
country.

The broker and result backend are both Redis, so no extra infrastructure
is needed beyond what the app already runs. Tasks are *synchronous* and
open their own short-lived sessions -- they never share the web app's
async engine or event loop.
"""

from __future__ import annotations

import os
from datetime import UTC
from typing import TYPE_CHECKING

import redis
from celery import Celery
from celery.schedules import crontab

from app.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = get_logger(__name__)

_settings = get_settings()

celery_app = Celery(
    "shurly",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.tasks.celery_app"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Analytics are at-least-once and idempotent; ack late so a worker
    # crash re-delivers rather than drops the event.
    task_acks_late=True,
    task_time_limit=30,
    worker_max_tasks_per_child=10_000,
)

celery_app.conf.beat_schedule = {
    "rollup-click-counts-hourly": {
        "task": "shurly.rollup_click_counts",
        "schedule": crontab(minute=0),
    },
    "enrich-geoip-hourly": {
        "task": "shurly.enrich_geoip",
        "schedule": crontab(minute=5),
    },
}


def _sync_db_url() -> str:
    """Return a *synchronous* SQLAlchemy URL for worker use."""
    url = _settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url.replace("+asyncpg", "")


def _open_sync_session() -> Session:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    return Session(create_engine(_sync_db_url(), pool_pre_ping=True))


def _clean(value: str | None, max_len: int) -> str | None:
    if not value:
        return None
    return value[:max_len]


@celery_app.task(name="shurly.record_click", bind=True)
def record_click(
    self: object,
    *,
    short_code: str,
    referrer: str | None,
    user_agent: str | None,
    ip: str | None,
) -> dict[str, object]:
    """Persist a single click event (fire-and-forget path).

    Appends an ``analytics`` row and bumps the denormalized
    ``urls.click_count``. Never raises for a missing URL -- an expired or
    deleted code simply yields a no-op so a burst of stale clicks can't
    crash the worker.
    """
    from sqlalchemy import select

    from app.models import Analytics, Url
    from app.models.base import utcnow

    with _open_sync_session() as session:
        urlrow = session.execute(
            select(Url).where(Url.short_code == short_code)
        ).scalar_one_or_none()
        if urlrow is None:
            log.warning("click_for_missing_url", code=short_code)
            return {"status": "skipped", "reason": "missing_url"}

        session.add(
            Analytics(
                url_id=urlrow.id,
                clicked_at=utcnow(),
                referrer=_clean(referrer, 2048),
                user_agent=_clean(user_agent, 1024),
                ip_address=_clean(ip, 45),
                country=None,
            )
        )
        urlrow.click_count = (urlrow.click_count or 0) + 1
        session.commit()
    return {"status": "recorded", "code": short_code}


@celery_app.task(name="shurly.rollup_click_counts")
def rollup_click_counts() -> dict[str, object]:
    """Hourly reconciliation of Redis click counters into PostgreSQL.

    For each ``shurly:clicks:*`` key with a positive counter, add the
    delta to ``urls.click_count`` and reset the key. This keeps the
    denormalized counter accurate even for clicks only counted in Redis.

    Runs in a synchronous worker, so it opens its own *synchronous* Redis
    client (the web app's ``RedisCache`` is ``redis.asyncio`` and cannot be
    driven from a sync task).
    """
    from sqlalchemy import update

    from app.models import Url

    client = redis.Redis.from_url(_settings.redis_url, decode_responses=True)
    reconciled = 0
    try:
        for key in client.keys("shurly:clicks:*"):
            raw = client.get(key)
            if not raw:
                continue
            try:
                delta = int(raw)
            except (TypeError, ValueError):
                continue
            if delta <= 0:
                continue
            code = str(key).split(":")[-1]
            with _open_sync_session() as session:
                session.execute(
                    update(Url)
                    .where(Url.short_code == code)
                    .values(click_count=Url.click_count + delta)
                )
                session.commit()
            client.set(key, "0")
            reconciled += 1
    finally:
        client.close()
    log.info("rollup_click_counts_done", reconciled=reconciled)
    return {"reconciled": reconciled}


@celery_app.task(name="shurly.enrich_geoip")
def enrich_geoip() -> dict[str, object]:
    """Backfill ``analytics.country`` for recent events using GeoIP.

    Uses the MaxMind GeoLite2 database at ``SHURLY_GEOIP_MMDB`` when
    present; otherwise it is a graceful no-op so the pipeline never
    requires the binary to run.
    """
    mmdb = os.environ.get("SHURLY_GEOIP_MMDB")
    if not mmdb or not os.path.exists(mmdb):
        log.info("geoip_skipped_no_database")
        return {"skipped": True}

    try:
        from geoip2.database import Reader  # type: ignore[import-not-found]
    except ImportError:
        log.info("geoip_skipped_no_reader")
        return {"skipped": True, "reason": "geoip2 not installed"}

    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.models import Analytics

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    enriched = 0
    with _open_sync_session() as session:
        rows = (
            session.execute(
                select(Analytics).where(
                    Analytics.country.is_(None),
                    Analytics.ip_address.is_not(None),
                    Analytics.clicked_at >= cutoff,
                )
            )
            .scalars()
            .all()
        )
        reader = Reader(mmdb)
        for row in rows:
            try:
                match = reader.country(row.ip_address)
                row.country = match.country.iso_code
                enriched += 1
            except Exception as exc:
                # A single bad row (e.g. malformed IP, no GeoIP match) must
                # not abort the whole batch -- log and keep going.
                log.debug("geoip_enrich_skipped", ip=row.ip_address, error=str(exc))
        session.commit()
    return {"enriched": enriched}


__all__ = ["celery_app"]
