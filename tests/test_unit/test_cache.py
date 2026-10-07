"""Unit tests for the Redis wrapper and circuit breaker (``app.cache``)."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from app.cache import CircuitBreaker, CircuitBreakerError, CircuitState


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        assert cb.state is CircuitState.CLOSED
        cb.check()  # no raise

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb.record_failure()
        assert cb.state is CircuitState.CLOSED
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        with pytest.raises(CircuitBreakerError):
            cb.check()

    def test_half_open_after_cooldown(self, monkeypatch):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10)
        clock = {"t": 100.0}
        monkeypatch.setattr("app.cache.time.monotonic", lambda: clock["t"])
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        clock["t"] = 111.0  # past cooldown
        assert cb.state is CircuitState.HALF_OPEN

    def test_success_resets(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb.record_failure()
        cb.record_success()
        assert cb.state is CircuitState.CLOSED

    async def test_call_records_success(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)

        async def ok():
            return 42

        assert await cb.call(ok) == 42
        assert cb.state is CircuitState.CLOSED

    async def test_call_records_failure(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)

        async def boom():
            raise aioredis.RedisError("nope")

        with pytest.raises(aioredis.RedisError):
            await cb.call(boom)
        # threshold=2: one failure keeps the circuit closed.
        assert cb.state is CircuitState.CLOSED

    async def test_call_blocks_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)

        async def boom():
            raise aioredis.RedisError("nope")

        with pytest.raises(aioredis.RedisError):
            await cb.call(boom)
        with pytest.raises(CircuitBreakerError):
            await cb.call(lambda: _coro("x"))


async def _coro(x):
    return x


class TestRedisCache:
    async def test_set_get(self, cache):
        await cache.set("k", "v", ex=10)
        assert await cache.get("k") == "v"

    async def test_get_missing(self, cache):
        assert await cache.get("nope") is None

    async def test_delete(self, cache):
        await cache.set("k", "v")
        assert await cache.delete("k") == 1
        assert await cache.get("k") is None

    async def test_exists(self, cache):
        assert await cache.exists("nope") is False
        await cache.set("k", "v")
        assert await cache.exists("k") is True

    async def test_incr(self, cache):
        assert await cache.incr("c") == 1
        assert await cache.incr("c") == 2
        assert await cache.incr("c", 5) == 7

    async def test_ping(self, cache):
        assert await cache.ping() is True

    async def test_json_roundtrip(self, cache):
        await cache.set_json("j", {"a": 1, "b": [1, 2]})
        assert await cache.get_json("j") == {"a": 1, "b": [1, 2]}
        assert await cache.get_json("missing") is None

    async def test_fail_open_when_breaker_open(self, cache):
        cache.breaker.record_failure()
        cache.breaker.record_failure()
        cache.breaker.record_failure()
        cache.breaker.record_failure()
        cache.breaker.record_failure()  # opens the breaker
        # get_url-style primitives raise CircuitBreakerError; the high-level
        # cache_repository catches it. Here we assert the primitive does.
        with pytest.raises(CircuitBreakerError):
            await cache.get("k")
