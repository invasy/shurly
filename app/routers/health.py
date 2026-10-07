"""``/health`` and ``/ready`` -- liveness/readiness probes.

``/health`` reports component status (database + redis) with latency and
returns ``200`` when the process is up, ``503`` when a hard dependency is
down. ``/ready`` is a strict all-or-nothing gate for load-balancer
readiness.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.cache import CircuitBreakerError, RedisCache
from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db import get_session_factory
from app.dependencies import get_cache
from app.schemas import ComponentHealth, HealthOut, ReadyOut

log = get_logger(__name__)

router = APIRouter(tags=["health"])


async def _check_db() -> tuple[str, float | None]:
    """Return (status, latency_ms) for the database."""
    start = time.perf_counter()
    try:
        session = get_session_factory()()
        try:
            await session.execute(text("SELECT 1"))
        finally:
            await session.close()
        return "ok", (time.perf_counter() - start) * 1000
    except Exception as exc:
        log.warning("health_db_error", error=str(exc))
        return "down", None


async def _check_redis(cache: RedisCache) -> tuple[str, float | None]:
    """Return (status, latency_ms) for Redis (bypassing the breaker)."""
    start = time.perf_counter()
    try:
        ok = await cache.client.ping()
        return ("ok" if ok else "down"), (time.perf_counter() - start) * 1000
    except (CircuitBreakerError, OSError) as exc:
        log.warning("health_redis_error", error=str(exc))
        return "down", None


@router.get("/health", response_model=HealthOut, summary="Liveness + component health")
async def health(
    cache: RedisCache = Depends(get_cache), settings: Settings = Depends(get_settings)
) -> HealthOut:
    """Report overall and per-component health with latency."""
    db_status, db_ms = await _check_db()
    redis_status, redis_ms = await _check_redis(cache)
    overall = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"
    return HealthOut(
        status=overall,
        version=settings.app_name,
        database=ComponentHealth(status=db_status, latency_ms=db_ms),
        redis=ComponentHealth(status=redis_status, latency_ms=redis_ms),
        checks={"db": db_status, "redis": redis_status},
    )


@router.get("/ready", response_model=ReadyOut, summary="Readiness gate")
async def ready(cache: RedisCache = Depends(get_cache)) -> ReadyOut:
    """All-or-nothing readiness for load-balancer / k8s probes."""
    db_status, _ = await _check_db()
    redis_status, _ = await _check_redis(cache)
    up = db_status == "ok" and redis_status == "ok"
    return ReadyOut(
        status="ok" if up else "down",
        database=db_status == "ok",
        redis=redis_status == "ok",
    )
