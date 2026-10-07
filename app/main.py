"""FastAPI application factory for shurly.

``create_app`` wires together configuration, middleware, routers, and
exception handlers and returns a fully-formed :class:`fastapi.FastAPI`
instance. A module-level ``app`` is provided for convenience (``uvicorn
app.main:app``) and for tests.

The factory is idempotent-friendly: calling it twice yields independent
apps (middleware and exception handlers are attached per-instance).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.core.exceptions import (
    ShurlyError,
)
from app.core.logging import get_logger
from app.core.middleware import (
    CorrelationIdMiddleware,
    MetricsMiddleware,
    SecurityHeadersMiddleware,
    setup_prometheus,
)
from app.routers import admin, health, redirect, urls

log = get_logger(__name__)

_VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the shurly FastAPI application."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "shurly_starting",
            version=_VERSION,
            environment=settings.environment,
            code_strategy=settings.code_strategy,
            idempotent=settings.idempotent,
        )
        # Warm the DB pool and verify connectivity early so a misconfigured
        # deployment fails loudly at startup rather than on first request.
        from app.db import get_engine, get_session_factory

        get_engine(settings)
        session = get_session_factory()()
        try:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
            log.info("db_connected")
        except Exception as exc:
            log.warning("db_connect_failed", error=str(exc))
        finally:
            await session.close()
        yield
        from app.db import dispose_engine

        await dispose_engine()
        log.info("shurly_stopped")

    app = FastAPI(
        title="shurly",
        version=_VERSION,
        description="Scalable URL shortener service (bit.ly-style).",
        openapi_url="/openapi.json",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # --- middleware (outermost listed first) ---
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # --- routers ---
    app.include_router(health.router)
    app.include_router(urls.router)
    app.include_router(admin.router)
    # Redirect is last and broadest: it catches any ``/{short_code}``.
    app.include_router(redirect.router)

    # --- Prometheus ---
    setup_prometheus(app, _VERSION)

    # --- exception handlers ---
    _register_exception_handlers(app)

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions to clean JSON HTTP responses."""

    @app.exception_handler(ShurlyError)
    async def _shurly_handler(request: Request, exc: ShurlyError) -> JSONResponse:
        status_code = exc.http_status
        body = {"error": exc.error_code, "detail": exc.detail}
        headers = dict(exc.headers)
        # Echo the correlation id for traceability.
        corr = getattr(request.state, "correlation_id", None)
        if corr:
            headers["X-Correlation-Id"] = corr
        if status_code >= 500:  # noqa: PLR2004
            log.error("request_failed", status=status_code, code=exc.error_code)
        return JSONResponse(status_code=status_code, content=body, headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", error=repr(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": "internal server error"},
        )


app = create_app()


def run() -> None:
    """Entry point for ``python -m app.main`` / the Docker CMD."""
    host = os.environ.get("SHURLY_HOST", "0.0.0.0")
    port = int(os.environ.get("SHURLY_PORT", "8000"))
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=int(os.environ.get("SHURLY_WORKERS", "1")),
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
