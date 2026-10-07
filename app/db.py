"""Async SQLAlchemy engine / session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_timeout=30,
        echo=settings.db_echo,
    )


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return (and lazily create) the process-wide async engine."""
    global _engine, _session_factory
    settings = settings or get_settings()
    if _engine is None:
        _engine = _build_engine(settings)
        _session_factory = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            autoflush=False,
        )
        log.info("db_engine_created", url=_engine.url.render_as_string(hide_password=True))
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return (and lazily create) the process-wide session factory."""
    get_engine(settings)
    assert _session_factory is not None
    return _session_factory


def set_engine(engine: AsyncEngine | None) -> None:
    """Replace (or clear) the engine -- used by tests."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = (
        async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        if engine is not None
        else None
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional scope around a series of operations."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Dispose of the engine (called on shutdown / in tests)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
