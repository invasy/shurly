"""Health / readiness / metrics schemas."""

from __future__ import annotations

from pydantic import BaseModel


class ComponentHealth(BaseModel):
    """Health of a single dependency (database / cache / queue)."""

    status: str  # "ok" | "degraded" | "down"
    latency_ms: float | None = None
    detail: str | None = None


class HealthOut(BaseModel):
    """``/health`` response."""

    status: str  # "ok" | "degraded" | "down"
    version: str
    database: ComponentHealth
    redis: ComponentHealth
    checks: dict[str, str]


class ReadyOut(BaseModel):
    """``/ready`` response (all hard deps up)."""

    status: str  # "ok" | "down"
    database: bool
    redis: bool
