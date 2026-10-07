"""API tests for ``/api/v1/urls`` (create / get / delete / stats)."""

from __future__ import annotations

import httpx


async def _create(client: httpx.AsyncClient, headers, **overrides) -> str:
    payload = {"url": "https://example.com/doc"}
    payload.update(overrides)
    resp = await client.post("/api/v1/urls", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["short_code"]


class TestCreate:
    async def test_create(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/a"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["original_url"] == "https://example.com/a"
        assert body["short_code"]
        assert body["short_url"] == f"http://testserver/{body['short_code']}"
        assert body["click_count"] == 0
        assert body["expires_at"] is None

    async def test_create_requires_api_key(self, client):
        resp = await client.post("/api/v1/urls", json={"url": "https://e.com/x"})
        assert resp.status_code == 401
        assert resp.json()["error"] == "authentication_required"
        assert resp.headers.get("WWW-Authenticate") == "ApiKey"

    async def test_create_bad_url(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/urls", json={"url": "ftp://e.com/x"}, headers=auth_headers
        )
        assert resp.status_code == 422
        # Rejected by the Pydantic schema validator (http/https only).
        assert "detail" in resp.json()

    async def test_create_idempotent(self, client, auth_headers):
        a = await _create(client, auth_headers, url="https://e.com/i")
        b = await _create(client, auth_headers, url="https://e.com/i")
        assert a == b

    async def test_custom_alias(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/urls",
            json={"url": "https://e.com/a", "custom_alias": "my-link"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["short_code"] == "my-link"

    async def test_custom_alias_invalid(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/urls",
            json={"url": "https://e.com/a", "custom_alias": "bad alias!"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_custom_alias_conflict(self, client, auth_headers):
        await _create(client, auth_headers, url="https://e.com/1", custom_alias="dup")
        resp = await client.post(
            "/api/v1/urls",
            json={"url": "https://e.com/2", "custom_alias": "dup"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "conflict"

    async def test_expires_in_sets_ttl(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/urls",
            json={"url": "https://e.com/ttl", "expires_in": 3600},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is not None


class TestGet:
    async def test_get(self, client, auth_headers):
        code = await _create(client, auth_headers)
        resp = await client.get(f"/api/v1/urls/{code}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["short_code"] == code
        assert body["custom_alias"] is False

    async def test_get_not_found(self, client):
        resp = await client.get("/api/v1/urls/nope")
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"


class TestDelete:
    async def test_delete(self, client, auth_headers):
        code = await _create(client, auth_headers)
        resp = await client.delete(f"/api/v1/urls/{code}", headers=auth_headers)
        assert resp.status_code == 204
        # Redirect now yields 410 Gone.
        resp2 = await client.get(f"/{code}")
        assert resp2.status_code == 410

    async def test_delete_not_found(self, client, auth_headers):
        resp = await client.delete("/api/v1/urls/nope", headers=auth_headers)
        assert resp.status_code == 404


class TestStats:
    async def test_stats_empty(self, client, auth_headers):
        code = await _create(client, auth_headers)
        resp = await client.get(f"/api/v1/urls/{code}/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["short_code"] == code
        assert body["total_clicks"] == 0
        assert body["countries"] == []
        assert body["top_referrers"] == []

    async def test_stats_not_found(self, client):
        resp = await client.get("/api/v1/urls/nope/stats")
        assert resp.status_code == 404
