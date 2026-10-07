"""Declarative base and shared mixins for all ORM models.

Types are kept *portable* so the exact same models run on PostgreSQL in
production and on SQLite in hermetic tests (see ``app/config.py`` and the
test-suite ``conftest``). The portable :class:`sqlalchemy.Uuid` type maps to
native ``UUID`` on PostgreSQL and ``CHAR(32)`` on SQLite; the portable
``INET``-as-``String(45)`` variant does the same for IP addresses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import BigInteger, DateTime, Integer, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Primary-key integer column type: ``BIGINT`` on PostgreSQL and ``INTEGER``
#: on SQLite. SQLite only auto-increments ``INTEGER`` primary keys, so a
#: plain ``BigInteger`` PK would insert ``NULL`` and violate NOT NULL in the
#: hermetic test database. PostgreSQL (production) is unaffected.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    """Declarative base for all shurly models.

    ``type_annotation_map`` lets us write ``Mapped[uuid.UUID]`` /
    ``Mapped[datetime]`` and have the correct, dialect-portable types chosen
    automatically.
    """

    type_annotation_map: Final = {
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


class TimestampMixin:
    """Adds ``created_at`` with a server-side default of now."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


def utcnow() -> datetime:
    """Timezone-aware UTC now (Python-side default)."""
    return datetime.now(UTC)
