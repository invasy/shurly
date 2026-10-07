"""The ``analytics`` table -- raw click events (append-only).

Written asynchronously (Celery fire-and-forget) so the redirect path never
blocks on analytics I/O. ``ip_address`` uses a portable ``String(45)``
variant (native ``INET`` on PostgreSQL) so the model works on every dialect.
``country`` is ISO 3166-1 alpha-2, populated by the GeoIP enrichment task.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntPK
from app.models.url import Url


class Analytics(Base):
    """A single click on a short URL."""

    __tablename__ = "analytics"
    __table_args__ = (
        Index("ix_analytics_url_id", "url_id"),
        Index("ix_analytics_clicked_at", "clicked_at"),
        Index("ix_analytics_referrer", "referrer"),
        Index("ix_analytics_country", "country"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    url_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("urls.id", ondelete="CASCADE"),
        nullable=False,
    )
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(
        String(45).with_variant(INET(), "postgresql"), nullable=True
    )
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    url: Mapped[Url] = relationship(back_populates="analytics")

    def __repr__(self) -> str:
        return f"<Analytics url_id={self.url_id} country={self.country!r}>"
