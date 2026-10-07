"""Async Redis wrapper with a circuit breaker.

The circuit breaker tracks consecutive failures; once the threshold is
reached it opens the circuit and all calls fail fast for a cooldown period
without touching the network. This keeps a Redis outage from adding latency
to the redirect hot path.
"""

from __future__ import annotations

import json
import time
from enum import StrEnum
from typing import Any, cast

import redis.asyncio as aioredis

from app.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """Raised when the Redis circuit is open (fail fast instead of piling on)."""


class CircuitBreaker:
    """Simple, thread-safe (asyncio) circuit breaker."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 15.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and (
            time.monotonic() - self._opened_at >= self._cooldown_seconds
        ):
            return CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            log.warning("circuit_breaker_opened", failures=self._consecutive_failures)

    def check(self) -> None:
        if self.state is CircuitState.OPEN:
            raise CircuitBreakerError("redis circuit breaker open")

    async def call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run *func*, tracking success/failure against the breaker."""
        self.check()
        was_half_open = self.state is CircuitState.HALF_OPEN
        try:
            result = await func(*args, **kwargs)
        except CircuitBreakerError:
            raise
        except (TimeoutError, aioredis.RedisError, OSError):
            self.record_failure()
            if self.state is CircuitState.OPEN and was_half_open:
                pass
            raise
        self.record_success()
        return result


class RedisCache:
    """High-level async Redis wrapper used by repositories."""

    def __init__(self, client: aioredis.Redis[Any], breaker: CircuitBreaker) -> None:
        self._client = client
        self._breaker = breaker

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 15.0,
    ) -> RedisCache:
        settings = settings or get_settings()
        client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=30,
        )
        breaker = CircuitBreaker(
            failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds
        )
        return cls(client, breaker)

    @property
    def client(self) -> aioredis.Redis[Any]:
        return self._client

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    # --- primitives (circuit-breaker protected) ---
    async def get(self, key: str) -> str | None:
        return cast(str | None, await self._breaker.call(self._client.get, key))

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        await self._breaker.call(self._client.set, key, value, ex=ex)

    async def delete(self, *keys: str) -> int:
        return cast(int, await self._breaker.call(self._client.delete, *keys))

    async def exists(self, key: str) -> bool:
        return bool(await self._breaker.call(self._client.exists, key))

    async def incr(self, key: str, amount: int = 1, *, ex: int | None = None) -> int:
        result = await self._breaker.call(self._client.incrby, key, amount)
        if ex is not None:
            await self._breaker.call(self._client.expire, key, ex)
        return int(result)

    async def ping(self) -> bool:
        return bool(await self._breaker.call(self._client.ping))

    # --- JSON helpers ---
    async def get_json(self, key: str) -> Any:
        raw = await self.get(key)
        return json.loads(raw) if raw is not None else None

    async def set_json(self, key: str, value: Any, *, ex: int | None = None) -> None:
        await self.set(key, json.dumps(value, ensure_ascii=False), ex=ex)

    # --- pipeline ---
    async def pipeline(self) -> _RedisPipeline:
        """Return a breaker-protected pipeline context manager."""
        return _RedisPipeline(self)

    async def close(self) -> None:
        await self._client.aclose()  # type: ignore[attr-defined]


class _RedisPipeline:
    """Minimal breaker-protected pipeline helper.

    Used for batch operations (e.g. purging multiple cache keys after a
    bulk invalidation). The breaker only guards the initial connectivity
    check; if Redis is down the underlying ``execute`` raises and the
    caller decides how to degrade.
    """

    def __init__(self, cache: RedisCache) -> None:
        self._cache = cache
        self._pipeline: aioredis.client.Pipeline[Any]

    async def __aenter__(self) -> aioredis.client.Pipeline[Any]:
        self._cache.breaker.check()
        self._pipeline = self._cache.client.pipeline()
        return self._pipeline

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                await self._pipeline.execute()
        finally:
            await self._pipeline.reset()
