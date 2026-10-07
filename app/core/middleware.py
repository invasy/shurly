"""ASGI middleware: correlation IDs, security headers, timing, metrics."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

from prometheus_client import (
    Counter,
    Histogram,
    Info,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import (
    bind_correlation_id,
    clear_correlation_id,
    get_logger,
    new_correlation_id,
)

log = get_logger(__name__)

# Prometheus instruments (module-level singletons).
REQUEST_COUNT = Counter(
    "shurly_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status"],
)
REQUEST_DURATION = Histogram(
    "shurly_request_duration_seconds",
    "HTTP request latency",
    ["method", "path_template", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
REDIRECTS_TOTAL = Counter("shurly_redirects_total", "Total redirects served")
URLS_CREATED_TOTAL = Counter("shurly_urls_created_total", "Total URLs created")
APP_INFO = Info("shurly", "Application information (version)")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id to every request and bind it to logs."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        corr_id = request.headers.get("X-Correlation-Id") or new_correlation_id()
        bind_correlation_id(corr_id)
        request.state.correlation_id = corr_id
        try:
            response = await call_next(request)
        finally:
            # Unbind so we don't leak the id into the next request on the
            # same worker task.
            clear_correlation_id()
        response.headers["X-Correlation-Id"] = corr_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add defensive security headers to every response."""

    _HEADERS: Final = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "X-XSS-Protection": "0",  # modern browsers deprecate the filter
    }

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for k, v in self._HEADERS.items():
            response.headers.setdefault(k, v)
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count + latency histograms in Prometheus."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        # Coarse bucketing by first two segments keeps label cardinality low.
        if path.startswith("/api/"):
            bucket = "/api/v1/urls" if "/urls" in path else "/api/v1/keys"
        elif path.startswith("/health") or path == "/metrics":
            bucket = path
        elif path.startswith("/"):
            bucket = "/redirect"
        else:
            bucket = path
        duration = time.perf_counter() - start
        REQUEST_COUNT.labels(
            method=request.method, path_template=bucket, status=str(response.status_code)
        ).inc()
        REQUEST_DURATION.labels(
            method=request.method, path_template=bucket, status=str(response.status_code)
        ).observe(duration)
        response.headers["X-Request-Duration-Ms"] = f"{duration * 1000:.2f}"
        return response


def setup_prometheus(app: object, version: str) -> None:
    """Register app-level Prometheus info and a custom /metrics route."""

    fastapi_app = app  # typed FastAPI at runtime
    APP_INFO.info({"version": version})

    @fastapi_app.get("/metrics", include_in_schema=False)  # type: ignore[attr-defined, untyped-decorator]
    async def metrics() -> Response:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            generate_latest,
        )

        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )
