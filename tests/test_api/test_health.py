"""API tests for ``/health`` and ``/ready``."""

from __future__ import annotations

import httpx


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"]["status"] == "ok"
    assert body["redis"]["status"] == "ok"
    assert body["checks"] == {"db": "ok", "redis": "ok"}
    assert isinstance(body["version"], str)
    # Latencies are reported (floats, may be 0).
    assert body["database"]["latency_ms"] is not None


async def test_ready_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["redis"] is True


async def test_health_carries_security_headers(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"


async def test_correlation_id_header(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Correlation-Id": "abc-123"})
    assert resp.headers.get("X-Correlation-Id") == "abc-123"
