"""API tests for ``/api/v1/keys`` (API key lifecycle)."""

from __future__ import annotations

ADMIN = "s3cr3t-admin-token"


class TestCreateKey:
    async def test_create(self, client):
        resp = await client.post("/api/v1/keys", json={"description": "svc", "rate_limit": 42})
        assert resp.status_code == 201
        body = resp.json()
        assert body["key"]
        assert body["key"].startswith("shurly-a-")
        assert body["rate_limit"] == 42
        assert body["description"] == "svc"
        assert body["id"]
        assert body["last_used_at"] is None

    async def test_create_default_rate_limit(self, client):
        resp = await client.post("/api/v1/keys", json={})
        assert resp.status_code == 201
        assert resp.json()["rate_limit"] == 100

    async def test_create_rejects_bad_rate_limit(self, client):
        resp = await client.post("/api/v1/keys", json={"rate_limit": 0})
        assert resp.status_code == 422


class TestListKeys:
    async def test_list_open_when_no_admin_token(self, client, api_key):
        resp = await client.get("/api/v1/keys")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) >= 1
        # Plaintext is never returned.
        for record in body:
            assert "key" not in record

    async def test_list_requires_admin_token(self, client, api_key, monkeypatch):
        monkeypatch.setenv("SHURLY_ADMIN_TOKEN", ADMIN)
        resp = await client.get("/api/v1/keys")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "invalid admin token"

    async def test_list_with_correct_admin_token(self, client, api_key, monkeypatch):
        monkeypatch.setenv("SHURLY_ADMIN_TOKEN", ADMIN)
        resp = await client.get("/api/v1/keys", headers={"X-Admin-Token": ADMIN})
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


class TestRevokeKey:
    async def _create_key_id(self, client) -> str:
        resp = await client.post("/api/v1/keys", json={"description": "doomed"})
        assert resp.status_code == 201
        return resp.json()["id"]

    async def test_revoke(self, client, api_key):
        key_id = await self._create_key_id(client)
        resp = await client.delete(f"/api/v1/keys/{key_id}")
        assert resp.status_code == 204

    async def test_revoke_not_found(self, client, api_key):
        resp = await client.delete("/api/v1/keys/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_revoke_requires_admin_token(self, client, api_key, monkeypatch):
        monkeypatch.setenv("SHURLY_ADMIN_TOKEN", ADMIN)
        key_id = await self._create_key_id(client)
        resp = await client.delete(f"/api/v1/keys/{key_id}")
        assert resp.status_code == 403

    async def test_revoke_bad_uuid(self, client, api_key):
        resp = await client.delete("/api/v1/keys/not-a-uuid")
        assert resp.status_code == 422
