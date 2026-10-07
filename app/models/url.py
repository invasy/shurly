"""The ``urls`` table -- the short_code -> original_url mapping.

The model is dialect-portable: on PostgreSQL ``user_id`` is a native ``UUID``
column and on SQLite it is ``CHAR(32)`` (both handled by ``sqlalchemy.Uuid``).

Idempotency ("same (url, user) -> same short code") is enforced in
application code rather than with a functional/``COALESCE`` index, so the
same definition works on every supported dialect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntPK, TimestampMixin, utcnow

if TYPE_CHECKING:
    from app.models.analytics import Analytics


class Url(TimestampMixin, Base):
    """A shortened URL.

    ``short_code`` is the compact identifier (e.g. ``aB3xK9``) that appears
    in the shortened URL path. It is globally unique and indexed.
    """

    __tablename__ = "urls"
    __table_args__ = (
        CheckConstraint("length(short_code) > 0", name="ck_urls_short_code_nonempty"),
        Index("ix_urls_original_url", "original_url"),
        Index("ix_urls_expires_at", "expires_at"),
        Index("ix_urls_user_id", "user_id"),
        Index("ix_urls_url_user", "original_url", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    short_code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    custom_alias: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    click_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    analytics: Mapped[list[Analytics]] = relationship(
        back_populates="url",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        # SQLite returns naive datetimes (UTC); normalize to aware so the
        # comparison works identically on PostgreSQL (aware) and SQLite.
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return exp <= utcnow()

    def __repr__(self) -> str:
        return f"<Url {self.short_code!r} -> {self.original_url!r}>"
