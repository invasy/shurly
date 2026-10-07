"""Unit tests for security primitives (``app.core.security``)."""

from __future__ import annotations

import app.core.security as sec
import pytest
import redis.asyncio as aioredis
from app.core.exceptions import RateLimitedError
from app.core.security import (
    SlidingWindowRateLimiter,
    generate_api_key,
    hash_api_key,
    ip_from_request,
    verify_api_key,
)


class TestApiKey:
    def test_generate_format(self):
        key = generate_api_key()
        assert key.startswith("shurly-a-")
        # 40 hex chars after the prefix.
        assert len(key) == len("shurly-a-") + 40

    def test_generate_unique(self):
        assert generate_api_key() != generate_api_key()

    def test_hash_verifies(self):
        key = generate_api_key()
        stored = hash_api_key(key)
        assert ":" in stored
        assert verify_api_key(key, stored) is True

    def test_hash_wrong_key_fails(self):
        key = generate_api_key()
        stored = hash_api_key(key)
        assert verify_api_key("shurly-a-" + "0" * 40, stored) is False

    def test_hash_is_sha256_prefix(self):
        import hashlib

        key = generate_api_key()
        stored = hash_api_key(key)
        digest = hashlib.sha256(key.encode()).hexdigest()
        assert stored.startswith(digest + ":")

    def test_verify_malformed_stored(self):
        assert verify_api_key("anything", "no-colon-here") is False


class TestRateLimiter:
    async def test_allows_within_limit(self, monkeypatch):
        monkeypatch.setattr(sec.time, "time", lambda: 1000.0)
        limiter = SlidingWindowRateLimiter(_FakeRedis())
        for _ in range(3):
            await limiter.check("k", limit=5, window_seconds=60)  # no raise

    async def test_denies_over_limit(self, monkeypatch):
        monkeypatch.setattr(sec.time, "time", lambda: 1000.0)
        limiter = SlidingWindowRateLimiter(_FakeRedis())
        for _ in range(5):
            await limiter.check("k", limit=5, window_seconds=60)
        with pytest.raises(RateLimitedError) as ei:
            await limiter.check("k", limit=5, window_seconds=60)
        assert ei.value.retry_after is not None and ei.value.retry_after >= 1
        assert ei.value.headers["Retry-After"] == str(ei.value.retry_after)

    async def test_window_expiry(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr(sec.time, "time", lambda: t["now"])
        limiter = SlidingWindowRateLimiter(_FakeRedis())
        for _ in range(5):
            await limiter.check("k", limit=5, window_seconds=60)
        # Advance past the window -> allowed again.
        t["now"] = 1000.0 + 61
        await limiter.check("k", limit=5, window_seconds=60)  # no raise

    async def test_fail_open_on_redis_error(self):
        limiter = SlidingWindowRateLimiter(_RaisingRedis())
        await limiter.check("k", limit=5, window_seconds=60)  # must not raise

    async def test_reset(self):
        limiter = SlidingWindowRateLimiter(_FakeRedis())
        await limiter.reset("k")


class TestIpFromRequest:
    def test_xff_first_hop(self, monkeypatch):
        req = _FakeRequest(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, client=None)
        assert ip_from_request(req) == "1.2.3.4"

    def test_client_host_fallback(self, monkeypatch):
        req = _FakeRequest(headers={}, client=_FakeClient("9.9.9.9"))
        assert ip_from_request(req) == "9.9.9.9"

    def test_unknown_when_no_client(self):
        req = _FakeRequest(headers={}, client=None)
        assert ip_from_request(req) == "unknown"


# --- minimal fakes --------------------------------------------------------
class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, headers, client):
        self.headers = headers
        self.client = client


class _FakeRedis:
    """Just enough surface for SlidingWindowRateLimiter.__init__."""

    def __init__(self) -> None:
        self._zsets: dict[str, list[float]] = {}

    async def delete(self, key: str) -> int:
        self._zsets.pop(key, None)
        return 1

    def register_script(self, script: str):
        async def _run(keys, args):
            # Emulate the Lua: trim the window, then admit or deny.
            # (args[3] is the member id, unused by this faithful-enough shim.)
            now, window, limit = args[0], args[1], args[2]
            zset = self._zsets.setdefault(keys[0], [])
            zset = [ts for ts in zset if ts > now - window]
            if len(zset) >= limit:
                return [-1, max(1, int(zset[0] + window - now)), len(zset) + 1]
            zset.append(now)
            self._zsets[keys[0]] = zset
            return [1, 0, len(zset)]

        return _run


class _RaisingRedis:
    def register_script(self, script: str):
        async def _run(keys, args):
            raise aioredis.RedisError("boom")

        return _run
