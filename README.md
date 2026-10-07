# shurly

**A scalable, production-ready URL shortener service** built with
modern Python.

`shurly` demonstrates system-design fundamentals and clean architecture: an
async **FastAPI** front-end over **PostgreSQL** (SQLAlchemy 2.0 + Alembic), a
**Redis** cache with cache-aside reads and a circuit breaker, a **Celery**
worker for analytics, API-key auth with per-key rate limiting, and a
Kubernetes/Helm deployment story. It targets **high read/write ratios**
(10K+ redirects/s, 1K+ creates/s), **zero short-code collisions**, and
**sub-50ms redirects** on cache hits.

```
                       ┌──────────────────────────────────────────────┐
                       │                   shurly                     │
                       │                                              │
  HTTPS ──────────────►│  FastAPI (async, uvicorn)                    │
   │                   │   ├─ GET /{code}        ── redirect hot path │
   │  create / stats / │   ├─ POST /api/v1/urls  ── write path        │
   │  keys             │   ├─ GET  /api/v1/urls/{code}/stats          │
   │                   │   └─ POST /api/v1/keys                      │
   │                   │                                              │
   │                   │  services → repositories                    │
   └───────────────────┼──────────────────────┬───────────────────────┤
                       │                      │                      │
                       │          cache-aside  │  fire-and-forget     │
                       │                      ▼                      ▼
                       │            ┌──────────────────┐  ┌──────────────────┐
                       └────────────┤      Redis       │  │  Celery worker   │
                                    │  (cache+ratelimit│  │  (analytics,     │
                                    │   +queue+locks)  │  │   GeoIP rollup)  │
                                    └────────┬─────────┘  └────────┬─────────┘
                                             │                    │
                                             ▼                    ▼
                                    ┌───────────────────────────────────┐
                                    │         PostgreSQL                │
                                    │  urls · analytics · api_keys      │
                                    └───────────────────────────────────┘
```

## Why these choices?

See **[docs/design.md](docs/design.md)** for the full trade-off discussion.
In short:

- **PostgreSQL** — ACID + a unique index give _zero_ collisions by
  construction; JSONB-friendly; `BIGINT` sequences make Base62 codes cheap.
- **Redis** — sub-ms cache-aside reads keep redirects under 50ms; a sliding
  window (Lua) gives atomic per-key rate limiting; a circuit breaker fails
  fast on outage.
- **Base62 over a DB sequence** — monotonic, collision-free, O(1) encode.
  A **Murmur3/Snowflake** strategy is also implemented for non-sequential,
  privacy-preserving codes (collision retried against the unique index).
- **Celery** — analytics are off the hot path; the redirect returns before
  any click is persisted.

## Features

| Capability     | Detail                                                                |
| -------------- | --------------------------------------------------------------------- |
| Shorten URL    | `POST /api/v1/urls` → unique Base62 (or Murmur3) code                 |
| Redirect       | `GET /{code}` → `307` (or `301` w/ `X-Stable`), sub-50ms on cache hit |
| Custom aliases | `custom_alias` field (validated, unique)                              |
| Analytics      | click count, referrer, user-agent, IP, GeoIP country                  |
| Expiration     | `expires_in` (seconds) → `410 Gone` after TTL                         |
| API keys       | hashed (SHA-256 + bcrypt), per-key rate limit                         |
| Rate limiting  | Redis sliding window per key (IP fallback)                            |
| Idempotency    | same `(url, user)` → same code (configurable)                         |
| Observability  | structured JSON logs + correlation IDs, Prometheus `/metrics`         |
| Resilience     | circuit breaker on Redis, graceful degradation of cache/queue         |

## Quick start (Docker Compose)

One command brings up PostgreSQL, Redis, the API, and the Celery worker:

```bash
cp .env.example .env        # adjust if needed
docker compose up --build
```

Then:

```bash
# 1. create an API key (plaintext shown once)
KEY=$(curl -s localhost:8000/api/v1/keys | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

# 2. create a short URL
curl -s -X POST localhost:8000/api/v1/urls \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"url":"https://example.com/very/long/path","custom_alias":"my-link","expires_in":3600}'
# → {"short_code":"my-link","short_url":"http://localhost:8000/my-link", ...}

# 3. redirect (follows to the original URL)
curl -sI localhost:8000/my-link | head -3
# → HTTP/1.1 307 Temporary Redirect  /  Location: https://example.com/very/long/path

# 4. stats
curl -s localhost:8000/api/v1/urls/my-link/stats
```

Interactive docs: <http://localhost:8000/docs>. Health: <http://localhost:8000/health>.
Metrics: <http://localhost:8000/metrics>.

### Development without Docker

```bash
uv sync                       # create .venv + install deps
docker compose up -d postgres redis
make migrate                  # alembic upgrade head
make dev                      # uvicorn with reload
make test                     # pytest (+ coverage)
```

## Project layout

```
shurly/
├── app/
│   ├── main.py               # FastAPI app factory + exception handlers
│   ├── config.py             # pydantic-settings (env-driven)
│   ├── db.py                 # async engine/session management
│   ├── cache.py              # Redis wrapper + circuit breaker
│   ├── dependencies.py       # FastAPI DI (services, auth, rate limit)
│   ├── models/               # SQLAlchemy 2.0 typed models
│   ├── schemas/              # Pydantic request/response models
│   ├── routers/              # urls / redirect / keys / health
│   ├── services/             # business logic (url, analytics, keys, encoding)
│   ├── repositories/         # DB + cache abstraction (swappable)
│   ├── tasks/                # Celery app + background jobs
│   └── core/                 # logging, security, exceptions, middleware
├── alembic/                  # migrations (async env)
├── helm/                     # Kubernetes chart
├── tests/                    # unit + API integration (pytest, httpx)
├── docs/                     # design, api, deployment
├── scripts/                  # init_db.sh, load-test helpers
├── compose.yml               # one-command local stack
└── pyproject.toml            # uv/PEP-621, ruff, mypy, pytest config
```

See **[AGENTS.md](AGENTS.md)** for how to work in this repo with an AI
coding agent, and **[docs/deployment.md](docs/deployment.md)** for running it
in Kubernetes with the bundled Helm chart.

## Testing

```bash
make test          # pytest with coverage gate (branch coverage)
make lint          # ruff + mypy (strict)
make fmt           # ruff format
```

Unit tests cover the Base62/Murmur3 encoders, rate limiter, and cache
repository; integration tests drive the FastAPI app through `httpx`
against a real (containerised) database + Redis, with per-test transaction
rollback for isolation.

## Performance

Redirects are cache-first: a Redis hit avoids the DB entirely and responds
in well under 50ms. Load-test with `hey`/`k6`/`locust` — see
[docs/deployment.md#load-testing](docs/deployment.md) for ready-made scripts.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
