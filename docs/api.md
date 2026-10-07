# shurly HTTP API

Base URL: `{SHURLY_BASE_URL}` (default `http://localhost:8000`).

The machine-readable schema is at [`/openapi.json`](#) (Swagger UI at `/docs`,
ReDoc at `/redoc`; both are disabled when `SHURLY_ENVIRONMENT=production`).
This page is a human-oriented walkthrough with representative examples.

## Conventions

* **Auth.** Write endpoints (`POST /api/v1/urls`, `DELETE /api/v1/urls/{code}`,
  `POST /api/v1/keys`) require an `X-API-Key` header when
  `SHURLY_API_KEYS_ENABLED=true`. Read endpoints (`GET /api/v1/urls/{code}`,
  `GET .../stats`, redirects) work without a key but accept one optionally.
* **Correlation id.** Every response carries `X-Correlation-Id`. Supply one in
  the request to continue an existing trace; otherwise one is generated.
* **Errors.** Domain errors use a uniform body:

  ```json
  { "error": "not_found", "detail": "short code 'xyz' not found" }
  ```

  Validation errors from request parsing use FastAPI's standard
  `{"detail": [...]}` shape.

| HTTP status | `error` code       | Meaning |
| --- | --- | --- |
| 201 | — | resource created |
| 204 | — | no content (successful delete/revoke) |
| 301 | — | permanent redirect (opt-in) |
| 307 | — | temporary redirect (default) |
| 401 | `authentication_required` | missing/invalid API key |
| 403 | — | admin action with a bad admin token |
| 404 | `not_found` | short code does not exist |
| 409 | `conflict` | duplicate alias / idempotent collision |
| 410 | `expired` | short code has passed its TTL |
| 422 | `invalid_url` / FastAPI detail | request failed validation |
| 429 | `rate_limited` | rate limit exceeded (see `Retry-After`) |
| 500 | `internal_error` | unexpected server error |
| 503 | `service_unavailable` | a backing dependency is down |

---

## URLs

### `POST /api/v1/urls` — create a short URL

Requires `X-API-Key`. Rate-limited.

Request:

```json
{
  "url": "https://example.com/very/long/path",
  "custom_alias": "my-link",
  "expires_in": 3600,
  "idempotent": true
}
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `url` | string | yes | Absolute `http://`/`https://`, ≤ 2048 chars. Loopback/private targets rejected unless `SHURLY_ALLOW_LOCALHOST`. |
| `custom_alias` | string | no | 1–64 chars, `[A-Za-z0-9][A-Za-z0-9_-]*`. Used verbatim as the code. |
| `expires_in` | int | no | TTL in seconds (≥ 1). `expires_at = now + TTL`. |
| `idempotent` | bool | no | Defaults to `SHURLY_IDEMPOTENT`. When true, the same `(url, user)` returns the same code instead of a new one. |

Response `201`:

```json
{
  "short_code": "my-link",
  "short_url": "http://localhost:8000/my-link",
  "original_url": "https://example.com/very/long/path",
  "created_at": "2024-09-30T12:00:00Z",
  "expires_at": "2024-09-30T13:00:00Z",
  "click_count": 0
}
```

### `GET /api/v1/urls/{short_code}` — metadata

Response `200`:

```json
{
  "id": 1234,
  "short_code": "my-link",
  "short_url": "http://localhost:8000/my-link",
  "original_url": "https://example.com/very/long/path",
  "custom_alias": true,
  "created_at": "2024-09-30T12:00:00Z",
  "expires_at": "2024-09-30T13:00:00Z",
  "click_count": 42
}
```

### `DELETE /api/v1/urls/{short_code}` — soft delete

Requires `X-API-Key`. Rate-limited. Expires the code in place; analytics are
preserved. Response `204` on success, `404` if unknown.

### `GET /api/v1/urls/{short_code}/stats` — analytics

Response `200`:

```json
{
  "short_code": "my-link",
  "total_clicks": 1000,
  "clicks_last_24h": 50,
  "clicks_last_7d": 300,
  "clicks_last_30d": 900,
  "countries": [
    { "country": "US", "count": 400 },
    { "country": "RU", "count": 350 }
  ],
  "top_referrers": [
    { "referrer": "https://news.ycombinator.com", "count": 120 }
  ],
  "first_click_at": "2024-09-30T12:00:05Z",
  "last_click_at": "2024-10-01T09:30:11Z"
}
```

`countries` and `top_referrers` are populated only when a GeoLite2 mmdb is
configured and clicks have been enriched.

---

## Redirect

### `GET /{short_code}`

The hot path. No key required.

* Default → **`307 Temporary Redirect`** with `Location: <original_url>`.
* With request header **`X-Stable: true`** → **`301 Moved Permanently`**.
* Unknown code → **`404`**. Expired code → **`410 Gone`**.

Every hit fires an analytics event (enqueued to Celery, or recorded
synchronously when `SHURLY_USE_CELERY=false`) *before* the response is sent,
so click persistence never adds to redirect latency.

```
$ curl -sI http://localhost:8000/my-link
HTTP/1.1 307 Temporary Redirect
location: https://example.com/very/long/path
x-correlation-id: 9f1c2a...
x-request-duration-ms: 1.42
```

---

## API keys

### `POST /api/v1/keys` — generate a key

Open (used to bootstrap the first key). Rate-limited. The plaintext is
**returned exactly once** and then unrecoverable.

Request:

```json
{ "description": "production service", "rate_limit": 1000 }
```

Response `201`:

```json
{
  "id": "9b2f...-...",
  "description": "production service",
  "rate_limit": 1000,
  "created_at": "2024-09-30T12:00:00Z",
  "last_used_at": null,
  "key": "shurly-a-0123abc..."
}
```

### `GET /api/v1/keys` — list keys

Requires `X-Admin-Token: <SHURLY_ADMIN_TOKEN>` (open when no admin token is
configured). Returns records **without** the plaintext.

### `DELETE /api/v1/keys/{key_id}` — revoke a key

Requires `X-Admin-Token`. `key_id` is the UUID from creation/list. Response
`204` on success, `404` if unknown, `422` if not a valid UUID.

---

## Health & metrics

### `GET /health` — liveness + component status

```json
{
  "status": "ok",
  "version": "shurly",
  "database": { "status": "ok", "latency_ms": 0.4 },
  "redis": { "status": "ok", "latency_ms": 0.2 },
  "checks": { "db": "ok", "redis": "ok" }
}
```

`status` is `ok` when both dependencies are up, `degraded` otherwise.

### `GET /ready` — readiness gate

All-or-nothing; use for load-balancer / Kubernetes readiness probes.

```json
{ "status": "ok", "database": true, "redis": true }
```

### `GET /metrics`

Prometheus text format: `shurly_requests_total`,
`shurly_request_duration_seconds` (histogram), `shurly_redirects_total`,
`shurly_urls_created_total`, and a `shurly_info` version label.
