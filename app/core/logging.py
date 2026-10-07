"""Structured logging with correlation IDs.

Uses ``structlog`` integrated with the stdlib ``logging`` machinery so that
both structlog events and third-party loggers (uvicorn, SQLAlchemy) render
through the same pipeline:

* **development**  -> a readable colour console renderer,
* **production**   -> compact JSON lines (one object per line).

A per-request ``correlation_id`` is bound into the ``structlog``
``contextvars`` by :mod:`app.core.middleware` and automatically merged into
every log line, giving end-to-end traceability.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import TYPE_CHECKING, cast

import structlog

if TYPE_CHECKING:
    from app.config import Settings

_configured = False


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging once per process."""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    is_dev = settings.environment != "production"

    if is_dev:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge: stdlib LogRecords (from uvicorn/SQLAlchemy/etc.) are funneled
    # through the same shared processors + renderer so output is uniform.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.processors.format_exc_info, renderer],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quieten chatty libraries unless in debug.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = True
    for name in ("sqlalchemy.engine",):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to *name*."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name if name else "shurly"))


def new_correlation_id() -> str:
    """Generate a correlation ID for a request that lacks one."""
    return uuid.uuid4().hex


def bind_correlation_id(corr_id: str) -> None:
    """Bind the correlation id to the structlog contextvars."""
    structlog.contextvars.bind_contextvars(correlation_id=corr_id)


def clear_correlation_id() -> None:
    """Unbind request-scoped context (correlation id, etc.)."""
    structlog.contextvars.clear_contextvars()
