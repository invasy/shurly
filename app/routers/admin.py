"""``/api/v1/keys`` -- API key management.

Generating and listing keys is a bootstrap / admin operation. In a
deployment these routes would be protected by an admin secret or placed
behind a private ingress; here ``POST /api/v1/keys`` is open (so you can
bootstrap the first key) while list/revoke require the ``X-Admin-Token``
header to match ``SHURLY_ADMIN_TOKEN``.
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.dependencies import enforce_rate_limit, get_api_key_service
from app.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from app.services import ApiKeyService

router = APIRouter(prefix="/api/v1/keys", tags=["keys"])


def _require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("SHURLY_ADMIN_TOKEN")
    if not expected:
        # No admin token configured -> treat the caller as admin (dev).
        return
    if x_admin_token != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid admin token",
        )


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new API key",
    dependencies=[Depends(enforce_rate_limit)],
)
async def create_key(
    payload: ApiKeyCreate,
    service: ApiKeyService = Depends(get_api_key_service),
) -> ApiKeyCreated:
    """Generate a new API key. The plaintext is returned exactly once."""
    result = await service.create(description=payload.description, rate_limit=payload.rate_limit)
    assert result.plaintext is not None
    return ApiKeyCreated(
        id=result.key.id,
        description=result.key.description,
        rate_limit=result.key.rate_limit,
        created_at=result.key.created_at,
        last_used_at=result.key.last_used_at,
        key=result.plaintext,
    )


@router.get("", response_model=list[ApiKeyOut], summary="List API keys")
async def list_keys(
    service: ApiKeyService = Depends(get_api_key_service),
    _admin: None = Depends(_require_admin),
) -> list[ApiKeyOut]:
    """List all API keys (never includes plaintext)."""
    records = await service.list()
    return [
        ApiKeyOut(
            id=r.id,
            description=r.description,
            rate_limit=r.rate_limit,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
        )
        for r in records
    ]


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_key(
    key_id: str,
    service: ApiKeyService = Depends(get_api_key_service),
    _admin: None = Depends(_require_admin),
) -> None:
    """Revoke (delete) an API key."""
    try:
        kid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="key_id must be a valid UUID",
        ) from None
    if not await service.revoke(kid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
