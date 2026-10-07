"""SQLAlchemy-backed URL repository with collision-safe insertion.

All reads/writes go through the async ORM. The repository is *flush-based*:
it never commits on its own. The unit of work (commit/rollback) is owned by
the request scope (see :func:`app.db.session_scope`), which keeps
transactions predictable and makes the repository trivially testable.

Collision safety has two independent layers:

* ``short_code`` is a unique index, so two concurrent inserts can never
  both win the same code -- the second one raises ``IntegrityError``.
* Idempotency is enforced in code: :meth:`find_by_url` deduplicates a
  ``(original_url, user)`` pair before insertion, and a concurrent race
  that still hits the database is recovered by re-reading the row after
  the failed flush (any unique-constraint violation).

Because the database serializes these, we need no application-level or
distributed locking for correctness.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import Analytics, Url
from app.models.base import utcnow

log = get_logger(__name__)


class UrlRepository:
    """Persistence for ``urls`` (and its ``analytics`` child table)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- reads ---
    async def get_by_code(self, short_code: str) -> Url | None:
        """Fetch a URL by its short code (regardless of expiration)."""
        result = await self._s.execute(select(Url).where(Url.short_code == short_code))
        return result.scalar_one_or_none()

    async def get_by_id(self, url_id: int) -> Url | None:
        result = await self._s.execute(select(Url).where(Url.id == url_id))
        return result.scalar_one_or_none()

    async def find_by_url(self, original_url: str, user_id: uuid.UUID | None = None) -> Url | None:
        """Idempotency lookup: does this (url, user) already have a code?

        ``user_id=None`` (anonymous) matches rows whose ``user_id`` is
        ``NULL``; a concrete UUID matches that exact owner. Rows that are
        expired or soft-deleted are filtered out so they can be replaced.
        """
        user_filter = Url.user_id.is_(None) if user_id is None else Url.user_id == user_id
        stmt = select(Url).where(Url.original_url == original_url).where(user_filter)
        result = await self._s.execute(stmt)
        for url in result.scalars().all():
            if not url.is_expired:
                return url
        return None

    # --- writes ---
    async def insert(self, url: Url) -> Url:
        """Insert a new URL (flush only; caller commits).

        Any integrity violation is left to the caller to interpret; this
        method simply flushes. Use :meth:`create_or_get` for the
        collision-safe create path.
        """
        self._s.add(url)
        await self._s.flush()
        return url

    async def create_or_get(
        self,
        *,
        original_url: str,
        short_code: str,
        user_id: uuid.UUID | None = None,
        custom_alias: bool = False,
        expires_in: int | None = None,
        idempotent: bool = True,
    ) -> tuple[Url, bool]:
        """Create a URL, or return the existing one when idempotent.

        Returns ``(url, created)``.

        1. If idempotent, look up an existing ``(url, user)``. If present
           and not expired, return it (``created=False``).
        2. Otherwise build the candidate row and flush (INSERT). The
           ``short_code`` unique index picks a single winner on
           concurrent races.
        3. On any integrity violation the (aborted) transaction is rolled
           back so the session is usable again, then -- when idempotent --
           the (url, user) is re-read and returned if a row won the race.
           Otherwise a :class:`ConflictError` is raised so the service
           layer regenerates the code and retries.

        No commit is performed here -- the request scope owns it. The
        internal rollback is required because a failed statement aborts
        the database transaction; it also means any earlier writes in the
        same request scope are undone, which is safe because
        ``create_or_get`` is the sole write in the create request.
        """
        if idempotent:
            existing = await self.find_by_url(original_url, user_id)
            if existing is not None and not existing.is_expired:
                return existing, False

        now = utcnow()
        expires_at = now + timedelta(seconds=expires_in) if expires_in is not None else None
        candidate = Url(
            short_code=short_code,
            original_url=original_url,
            custom_alias=custom_alias,
            user_id=user_id,
            expires_at=expires_at,
        )
        self._s.add(candidate)
        try:
            await self._s.flush()
        except IntegrityError as exc:
            await self._s.rollback()
            if idempotent:
                existing = await self.find_by_url(original_url, user_id)
                if existing is not None:
                    return existing, False
            raise ConflictError(f"short code {short_code!r} already in use") from exc
        return candidate, True

    # --- stats ---
    async def increment_clicks(self, short_code: str) -> int:
        """Atomically increment ``urls.click_count``; return the new total."""
        await self._s.execute(
            update(Url).where(Url.short_code == short_code).values(click_count=Url.click_count + 1)
        )
        await self._s.flush()
        row = (
            await self._s.execute(select(Url.click_count).where(Url.short_code == short_code))
        ).scalar_one_or_none()
        return int(row) if row is not None else 0

    async def add_analytics(self, event: Analytics) -> Analytics:
        """Persist one click event (flush only; caller commits)."""
        self._s.add(event)
        await self._s.flush()
        return event

    async def stats(
        self, short_code: str, *, top_countries: int = 10, top_referrers: int = 10
    ) -> dict[str, Any]:
        """Aggregate analytics for a short code into a stats payload."""
        url = await self.get_by_code(short_code)
        if url is None:
            raise NotFoundError(f"short code {short_code!r} not found")

        now = utcnow()
        d24, d7, d30 = (
            now - timedelta(hours=24),
            now - timedelta(days=7),
            now - timedelta(days=30),
        )

        total_row = (
            await self._s.execute(
                select(func.count(Analytics.id)).where(Analytics.url_id == url.id)
            )
        ).scalar_one()

        async def _count_since(ts: Any) -> int:
            r = await self._s.execute(
                select(func.count(Analytics.id))
                .where(Analytics.url_id == url.id)
                .where(Analytics.clicked_at >= ts)
            )
            return int(r.scalar_one())

        c24 = await _count_since(d24)
        c7 = await _count_since(d7)
        c30 = await _count_since(d30)

        country_rows = (
            await self._s.execute(
                select(Analytics.country, func.count(Analytics.id))
                .where(Analytics.url_id == url.id)
                .where(Analytics.country.is_not(None))
                .group_by(Analytics.country)
                .order_by(func.count(Analytics.id).desc())
                .limit(top_countries)
            )
        ).all()
        ref_rows = (
            await self._s.execute(
                select(Analytics.referrer, func.count(Analytics.id))
                .where(Analytics.url_id == url.id)
                .where(Analytics.referrer.is_not(None))
                .group_by(Analytics.referrer)
                .order_by(func.count(Analytics.id).desc())
                .limit(top_referrers)
            )
        ).all()
        bounds = (
            await self._s.execute(
                select(func.min(Analytics.clicked_at), func.max(Analytics.clicked_at)).where(
                    Analytics.url_id == url.id
                )
            )
        ).one()

        return {
            "short_code": short_code,
            "total_clicks": int(total_row or 0),
            "clicks_last_24h": c24,
            "clicks_last_7d": c7,
            "clicks_last_30d": c30,
            "countries": [{"country": c, "count": int(n)} for c, n in country_rows if c],
            "top_referrers": [{"referrer": r, "count": int(n)} for r, n in ref_rows if r],
            "first_click_at": bounds[0],
            "last_click_at": bounds[1],
        }

    # --- deletion ---
    async def soft_delete(self, short_code: str) -> bool:
        """Soft-delete by expiring the code immediately.

        We set ``expires_at`` in the past rather than removing the row,
        preserving analytics history and keeping the redirect deterministic
        (a subsequent hit returns 410 Gone). The row remains queryable.
        """
        now = utcnow()
        res = cast(
            CursorResult[Any],
            await self._s.execute(
                update(Url).where(Url.short_code == short_code).values(expires_at=now)
            ),
        )
        await self._s.flush()
        return (res.rowcount or 0) > 0


__all__ = ["UrlRepository"]
