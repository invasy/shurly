"""URL service: the business logic for creating, resolving, and managing
short URLs.

This is the orchestration layer: it validates input, chooses a code
strategy, drives the repository (DB) and cache (Redis) in a cache-aside
pattern, and applies policy (TTL, idempotency, alias rules).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.core.exceptions import (
    ConflictError,
    ExpiredError,
    NotFoundError,
    UrlValidationError,
)
from app.core.logging import get_logger
from app.models import Url
from app.repositories.cache_repository import CacheRepository, UrlCacheRecord
from app.repositories.url_repository import UrlRepository
from app.services.encoding import generate_code, validate_custom_alias

log = get_logger(__name__)

#: Hosts that are loopback/any-address; rejected unless explicitly allowed.
_LOOPBACK_HOSTS = frozenset({"localhost", "0.0.0.0", "127.0.0.1", "::1"})


@dataclass(slots=True)
class ResolvedUrl:
    """Result of resolving a short code for a redirect."""

    short_code: str
    original_url: str
    from_cache: bool
    expired: bool = False


class UrlService:
    """Orchestrates short-URL creation, redirect, and lifecycle."""

    def __init__(
        self,
        *,
        repo: UrlRepository,
        cache: CacheRepository,
        settings: Settings,
        next_sequence: Any,
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._settings = settings
        # ``next_sequence`` is an async callable returning the next int
        # from a monotonically increasing source (DB sequence by default).
        self._next_sequence = next_sequence

    # --- validation ---
    def validate_url(self, url: str) -> None:
        """Enforce URL policy: absolute http(s), length, no blocked hosts."""
        if not url:
            raise UrlValidationError("url must not be empty")
        if len(url) > self._settings.max_url_length:
            raise UrlValidationError(
                f"url exceeds maximum length of {self._settings.max_url_length}"
            )
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise UrlValidationError("url must be an absolute http(s) URL")

        # Reject obviously-unsafe hosts.
        host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if host in _LOOPBACK_HOSTS and not self._settings.allow_localhost:
            raise UrlValidationError(
                "localhost/loopback targets are not allowed in this environment"
            )
        if host.startswith("192.168.") or host.startswith("10.") or host.startswith("172.16."):
            raise UrlValidationError("private-network targets are not allowed")

    # --- create ---
    async def create(
        self,
        *,
        original_url: str,
        custom_alias: str | None = None,
        expires_in: int | None = None,
        idempotent: bool | None = None,
        user_id: uuid.UUID | None = None,
    ) -> tuple[Url, bool]:
        """Create (or idempotently return) a short URL.

        Returns ``(url, created)`` where ``created`` is False when an
        existing row was reused (idempotent hit).
        """
        self.validate_url(original_url)
        idempotent = self._settings.idempotent if idempotent is None else idempotent

        if custom_alias is not None:
            if not validate_custom_alias(custom_alias):
                raise UrlValidationError("custom_alias is invalid (must be URL-safe, start alnum)")
            # A custom alias must be unique; if taken by a *different* URL
            # we fail with a conflict. If it maps to the same URL we reuse.
            existing = await self._repo.get_by_code(custom_alias)
            if existing is not None:
                if existing.original_url == original_url:
                    return existing, False
                raise ConflictError(f"custom alias {custom_alias!r} is already taken")

        # Resolve the short code.
        if custom_alias is None:
            short_code = await self._allocate_code()
        else:
            short_code = custom_alias

        return await self._create_with_retry(
            original_url=original_url,
            short_code=short_code,
            custom_alias=custom_alias is not None,
            expires_in=expires_in,
            idempotent=idempotent,
            user_id=user_id,
        )

    async def _allocate_code(self) -> str:
        """Allocate the next short code per the configured strategy.

        For ``base62`` we draw from a monotonically increasing source
        (a DB sequence by default); for ``murmur3`` we hash a Snowflake
        seed. In both cases the DB unique index is the final arbiter.
        """
        if self._settings.code_strategy == "base62":
            seq = await self._next_sequence()
            return generate_code(self._settings, sequence_value=seq)
        return generate_code(self._settings)

    async def _create_with_retry(
        self,
        *,
        original_url: str,
        short_code: str,
        custom_alias: bool,
        expires_in: int | None,
        idempotent: bool,
        user_id: uuid.UUID | None,
        max_attempts: int = 3,
    ) -> tuple[Url, bool]:
        """Call the repository, retrying on short-code collisions."""
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return await self._repo.create_or_get(
                    original_url=original_url,
                    short_code=short_code,
                    user_id=user_id,
                    custom_alias=custom_alias,
                    expires_in=expires_in,
                    idempotent=idempotent,
                )
            except ConflictError as exc:
                last_exc = exc
                log.warning(
                    "short_code_collision",
                    code=short_code,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                # Regenerate a fresh code and retry.
                short_code = await self._allocate_code()
        assert last_exc is not None
        raise last_exc

    # --- resolve (redirect) ---
    async def resolve(self, short_code: str) -> ResolvedUrl:
        """Resolve a short code to its original URL (cache-aside).

        Raises :class:`NotFoundError` if unknown and
        :class:`ExpiredError` if the code has expired.
        """
        # Fast path: Redis.
        cached = await self._cache.get_url(short_code)
        if cached is not None:
            return ResolvedUrl(
                short_code=short_code,
                original_url=cached.original_url,
                from_cache=True,
            )

        # Negative-cache pre-check (poor-man's bloom filter): skip the DB
        # for codes we recently determined are absent.
        if await self._cache.is_known_missing(short_code):
            raise NotFoundError(f"short code {short_code!r} not found")

        # Slow path: database.
        try:
            url = await self._repo.get_by_code(short_code)
        except SQLAlchemyError as exc:
            log.error("db_read_failed", error=str(exc), code=short_code)
            raise NotFoundError(f"short code {short_code!r} not found") from exc

        if url is None:
            await self._cache.mark_missing(short_code)
            raise NotFoundError(f"short code {short_code!r} not found")

        if url.is_expired:
            await self._cache.invalidate(short_code)
            raise ExpiredError(f"short code {short_code!r} has expired")

        # Write-through to cache for subsequent hot reads.
        await self._cache.set_url(
            UrlCacheRecord(short_code=short_code, original_url=url.original_url)
        )
        return ResolvedUrl(
            short_code=short_code,
            original_url=url.original_url,
            from_cache=False,
        )

    # --- lifecycle ---
    async def soft_delete(self, short_code: str) -> bool:
        """Expire (soft-delete) a short code and purge its cache entries."""
        deleted = await self._repo.soft_delete(short_code)
        await self._cache.invalidate(short_code)
        if deleted:
            log.info("url_soft_deleted", code=short_code)
        return deleted

    async def get(self, short_code: str) -> Url:
        """Return the live URL row for a short code."""
        url = await self._repo.get_by_code(short_code)
        if url is None:
            raise NotFoundError(f"short code {short_code!r} not found")
        return url

    async def get_or_404(self, short_code: str) -> Url:
        """Return the URL row or raise NotFound/Expired."""
        url = await self._repo.get_by_code(short_code)
        if url is None:
            raise NotFoundError(f"short code {short_code!r} not found")
        if url.is_expired:
            raise ExpiredError(f"short code {short_code!r} has expired")
        return url
