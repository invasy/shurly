"""Security primitives: API key hashing and sliding-window rate limiting."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import bcrypt
import redis.asyncio as aioredis
from starlette.requests import Request

from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

log = get_logger(__name__)

_KEY_PREFIX = "shurly"
_KEY_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate_api_key() -> str:
    """Generate a random 40-character display-form API key.

    The key is returned in a stable, URL-friendly form::

        shurly-a-<40 hex chars>

    Only its SHA-256 hash is ever stored (see :func:`hash_api_key`).
    """
    raw = secrets.token_hex(20)
    return f"{_KEY_PREFIX}-{_KEY_ALPHABET[0]}-{raw}"


def hash_api_key(plaintext: str) -> str:
    """Return the stored form of an API key as ``<sha256>:<bcrypt>``.

    A bcrypt hash alone cannot be used for lookups (it is salted), so we
    store two things joined by ``:``:

    * ``sha256(plaintext)`` -- a fast, one-way digest used as the unique,
      indexed ``key_hash`` column for O(1) lookups.
    * ``bcrypt(sha256(plaintext))`` -- a salted proof-of-possession hash,
      so a leaked ``key_hash`` column still cannot be reversed.
    """
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    # bcrypt truncates input to 72 bytes; a sha256 hex digest fits fine.
    bcrypt_hash = bcrypt.hashpw(digest.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    return f"{digest}:{bcrypt_hash}"


def verify_api_key(plaintext: str, stored: str) -> bool:
    """Verify a plaintext API key against its stored ``digest:bcrypt`` form."""
    try:
        _, bcrypt_hash = stored.split(":", 1)
    except ValueError:
        return False
    candidate = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return bcrypt.checkpw(candidate.encode("utf-8"), bcrypt_hash.encode("ascii"))


class SlidingWindowRateLimiter:
    """Redis sliding-window rate limiter.

    Uses a sorted set per key: each request adds a member (timestamp) and
    the count of members inside the window determines allowance. The whole
    check-and-insert runs as one Lua script so it is atomic across workers.

    Falls open (allows the request) when Redis is unavailable, logging the
    failure -- the circuit-breaker philosophy: rate limiting must never
    take the redirect path down.
    """

    _LUA = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local member = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    if count >= limit then
        local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
        local retry = tonumber(oldest[2]) + window - now
        if retry < 1 then retry = 1 end
        return {-1, retry, count + 1}
    end
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window)
    return {1, 0, count + 1}
    """

    def __init__(self, redis: aioredis.Redis[Any]) -> None:
        self._redis = redis
        self._script = self._redis.register_script(self._LUA)

    async def check(self, key: str, limit: int, window_seconds: int) -> None:
        """Allow the request or raise :class:`RateLimitedError`."""
        key = f"shurly:rl:{key}"
        now = time.time()
        member = f"{now:.6f}-{secrets.token_hex(4)}"
        try:
            result = await self._script(keys=[key], args=[now, window_seconds, limit, member])
        except (aioredis.RedisError, OSError) as exc:
            log.warning("rate_limiter_unavailable", error=str(exc), key=key)
            return
        allowed, retry_after, _count = int(result[0]), int(result[1]), int(result[2])
        if allowed < 0:
            raise RateLimitedError(
                "rate limit exceeded",
                retry_after=max(1, retry_after),
            )

    async def reset(self, key: str) -> None:
        """Remove any accumulated tokens (used by tests/admin)."""
        await self._redis.delete(f"shurly:rl:{key}")


def ip_from_request(request: Request) -> str:
    """Best-effort client IP (honours ``X-Forwarded-For`` first hop)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
