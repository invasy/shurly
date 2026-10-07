"""Unit tests for the API key service (``app.services.api_key_service``)."""

from __future__ import annotations

import uuid

import pytest
from app.core.exceptions import AuthenticationError


class TestCreate:
    async def test_create_returns_plaintext(self, api_key_service):
        result = await api_key_service.create(description="k", rate_limit=5)
        assert result.plaintext is not None
        assert result.plaintext.startswith("shurly-a-")
        assert result.key.rate_limit == 5
        assert result.key.id is not None

    async def test_create_uses_default_rate_limit(self, api_key_service):
        result = await api_key_service.create()
        assert result.key.rate_limit == api_key_service._settings.default_rate_limit


class TestVerify:
    async def test_roundtrip(self, api_key_service):
        result = await api_key_service.create()
        record = await api_key_service.verify(result.plaintext)
        assert record.id == result.key.id

    async def test_wrong_key(self, api_key_service):
        await api_key_service.create()
        with pytest.raises(AuthenticationError):
            await api_key_service.verify("shurly-a-" + "0" * 40)

    async def test_unknown_key(self, api_key_service):
        with pytest.raises(AuthenticationError):
            await api_key_service.verify("shurly-a-" + "1" * 40)


class TestListRevoke:
    async def test_list(self, api_key_service):
        a = await api_key_service.create()
        b = await api_key_service.create()
        records = await api_key_service.list()
        ids = {r.id for r in records}
        assert a.key.id in ids and b.key.id in ids

    async def test_revoke(self, api_key_service):
        a = await api_key_service.create()
        assert await api_key_service.revoke(a.key.id) is True
        with pytest.raises(AuthenticationError):
            await api_key_service.verify(a.plaintext)
        assert await api_key_service.revoke(a.key.id) is False

    async def test_revoke_missing(self, api_key_service):
        assert await api_key_service.revoke(uuid.uuid4()) is False
