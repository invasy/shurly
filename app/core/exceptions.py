"""Application-specific exceptions.

Domain exceptions are raised by services/repositories and translated into
HTTP responses by the exception handlers registered in :mod:`app.main`
(see :func:`register_exception_handlers`).
"""

from __future__ import annotations

from typing import Any


class ShurlyError(Exception):
    """Base class for all shurly domain errors."""

    http_status: int = 500
    error_code: str = "internal_error"

    def __init__(self, detail: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers or {}

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.error_code, "detail": self.detail}


class NotFoundError(ShurlyError):
    """Short code does not exist (or is not visible to the caller)."""

    http_status = 404
    error_code = "not_found"


class ConflictError(ShurlyError):
    """Resource conflict (duplicate custom alias, duplicate URL, etc.)."""

    http_status = 409
    error_code = "conflict"


class UrlValidationError(ShurlyError):
    """The submitted URL is invalid or not allowed by policy."""

    http_status = 422
    error_code = "invalid_url"


class RateLimitedError(ShurlyError):
    """Client exceeded its rate limit."""

    http_status = 429
    error_code = "rate_limited"

    def __init__(
        self,
        detail: str = "rate limit exceeded",
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.retry_after = retry_after
        if retry_after is not None:
            self.headers = {"Retry-After": str(retry_after)}


class AuthenticationError(ShurlyError):
    """Missing or invalid API key."""

    http_status = 401
    error_code = "authentication_required"

    def __init__(self, detail: str = "valid API key required") -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "ApiKey"})


class ServiceUnavailableError(ShurlyError):
    """A backing service (database/cache/queue) is unavailable."""

    http_status = 503
    error_code = "service_unavailable"


class ExpiredError(ShurlyError):
    """Short code exists but has expired."""

    http_status = 410
    error_code = "expired"
