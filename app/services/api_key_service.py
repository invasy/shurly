"""API key management: generation, verification, and rate limits.

Keys are generated with a cryptographic RNG, shown to the user exactly
once, and stored only as ``sha256:bcrypt`` hashes. Verification is O(1) on
the indexed ``key_hash`` column followed by a bcrypt check.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.security import generate_api_key, hash_api_key, verify_api_key
from app.models import ApiKey

log = get_logger(__name__)


@dataclass(slots=True)
class AuthResult:
    """Outcome of authenticating an API key."""

    key: ApiKey
    plaintext: str | None = None  # populated only at creation time


class ApiKeyService:
    """CRUD + verification for API keys."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._s = session
        self._settings = settings

    async def create(
        self,
        *,
        description: str | None = None,
        rate_limit: int | None = None,
    ) -> AuthResult:
        """Generate a new API key. Returns the record and the plaintext."""
        plaintext = generate_api_key()
        record = ApiKey(
            id=uuid.uuid4(),
            key_hash=hash_api_key(plaintext),
            rate_limit=rate_limit or self._settings.default_rate_limit,
            description=description,
        )
        self._s.add(record)
        await self._s.flush()
        log.info("api_key_created", key_id=str(record.id), rate_limit=record.rate_limit)
        return AuthResult(key=record, plaintext=plaintext)

    async def verify(self, plaintext: str) -> ApiKey:
        """Verify a plaintext key; return the record or raise 401.

        The lookup is on the indexed SHA-256 digest (one row, at most),
        then bcrypt confirms proof of possession.
        """
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        # Stored form is ``digest:bcrypt``; prefix-match to find the row.
        stmt = select(ApiKey).where(ApiKey.key_hash.startswith(digest + ":")).limit(1)
        result = await self._s.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not verify_api_key(plaintext, record.key_hash):
            raise AuthenticationError("invalid API key")
        return record

    async def get_by_id(self, key_id: uuid.UUID) -> ApiKey | None:
        result = await self._s.execute(select(ApiKey).where(ApiKey.id == key_id))
        return result.scalar_one_or_none()

    async def list(self, *, limit: int = 100, offset: int = 0) -> list[ApiKey]:
        result = await self._s.execute(
            select(ApiKey).order_by(ApiKey.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def revoke(self, key_id: uuid.UUID) -> bool:
        """Delete (revoke) a key. Returns True if a row was removed."""
        record = await self.get_by_id(key_id)
        if record is None:
            return False
        await self._s.delete(record)
        await self._s.flush()
        log.info("api_key_revoked", key_id=str(key_id))
        return True
