"""The ``api_keys`` table -- API keys are stored only as hashes.

The primary key is a :class:`sqlalchemy.Uuid` column (native ``UUID`` on
PostgreSQL, ``CHAR(32)`` on SQLite). ``key_hash`` is
``sha256(plaintext):bcrypt(sha256(plaintext))`` -- the plaintext is only
returned once, at creation. The ``sha256`` half is the indexed lookup
column; the ``bcrypt`` half is the proof-of-possession value.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApiKey(TimestampMixin, Base):
    """An API key used for authenticated writes."""

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String(160), nullable=False)
    rate_limit: Mapped[int] = mapped_column(BigInteger, default=100, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id} rate_limit={self.rate_limit}>"
