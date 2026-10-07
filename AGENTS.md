# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

`shurly` is a scalable, bit.ly-style URL shortener: async **FastAPI** over
**PostgreSQL** (SQLAlchemy 2.0 + Alembic), **Redis** cache-aside + circuit
breaker + sliding-window rate limiting, **Celery** analytics worker, and a
Helm chart. Two code-generation strategies are implemented (Base62 over a DB
sequence, and Murmur3 hash) — see [docs/design.md](docs/design.md) for the
trade-offs.

## The command that matters

```bash
uv run pytest --cov=app
```

- The suite must stay **green** and above **80% branch coverage**
  (`pyproject.toml` enforces the gate via `pytest-cov`; `make test-cov` makes
  it a hard failure).
- Tests are **hermetic**: they run against **SQLite** (`aiosqlite`) and
  **`fakeredis`** (with `lupa` for Lua scripts) — no containers, no network.
  Do **not** add tests that require a live Postgres/Redis/Celery.
- The app is driven in tests through `httpx.ASGITransport` against the
  `create_app()` factory, with dependencies injected via
  `app.dependency_overrides` / the fixtures in [tests/conftest.py](tests/conftest.py).

## Conventions

- **Type hints everywhere.** `mypy` runs in `strict` mode (see `[tool.mypy]`).
  `make typecheck` / `make lint` must pass before you consider work done.
- **Async/await** for all I/O (DB, Redis, HTTP). The engine and session
  factories are process singletons in [app/db.py](app/db.py); services receive
  a session, they don't create engines.
- **Layering:** `routers → services → repositories → models`. Routers stay
  thin and declarative; business logic lives in `app/services`; all DB/Redis
  access goes through the repository abstractions in `app/repositories` so
  the backends are swappable (and so tests can fake them).
- **Dialect portability:** models use SQLAlchemy variant types (`Uuid`,
  `BigIntPK`, `INET`-as-`String(45)`) so the *same* code runs on PostgreSQL
  and SQLite. Keep them portable — avoid Postgres-only constructs in models.
- **Config** is env-driven via pydantic-settings with the `SHURLY_` prefix
  ([app/config.py](app/config.py)); add new settings there, never as bare
  `os.environ` reads in app code (the one exception is `SHURLY_ADMIN_TOKEN`
  in the admin router, which is read at request time on purpose).

## Gotchas (read these before touching the relevant file)

- **FastAPI `APIKeyHeader.__call__` is async.** Any call to
  `_api_key_header(request)` in [app/dependencies.py](app/dependencies.py)
  **must be awaited**.
- **SQLite returns naive datetimes**; `utcnow()` is aware. Compare against
  naive values only after normalising (see `Url.is_expired`).
- **Import shadowing:** `from app.tasks import celery_app` returns the Celery
  *instance*, not the module, because `app/tasks/__init__.py` re-exports it.
  Import the real module as `from app.tasks.celery_app import celery_app`.
- **Redirects are fire-and-forget.** `AnalyticsService.dispatch_click` uses
  Celery when `SHURLY_USE_CELERY` is true and an `asyncio` background task
  otherwise; tests monkeypatch it to the synchronous `record_click` path for
  determinism.
- **`httpx.ASGITransport` does not run lifespan**, so the DB engine/Redis are
  set up by fixtures (see `tests/conftest.py`), not by app startup.

## Layout (quick map)

```
app/
  main.py            app factory, middleware, exception handlers, /metrics
  config.py          Settings (pydantic-settings, SHURLY_ prefix)
  db.py              async engine + session factory (process singletons)
  cache.py           Redis wrapper + circuit breaker
  dependencies.py    FastAPI DI: services, repos, auth, rate limiting
  models/            SQLAlchemy 2.0 typed models (url, analytics, api_key)
  schemas/           Pydantic request/response models
  routers/           urls / redirect / admin(keys) / health
  services/          url / analytics / api_key / encoding
  repositories/      url_repository (DB) + cache_repository (Redis)
  tasks/             Celery app + analytics/GeoIP jobs
  core/              logging, security, exceptions, middleware
tests/
  conftest.py        fixtures (SQLite + fakeredis, app, client, services)
  test_unit/         encoding, security, cache, repositories, services
  test_api/          urls / redirect / keys / health via httpx
```

## Do / don't

- **Do** add a test for every behaviour you change; keep coverage ≥ 80%.
- **Do** keep the OpenAPI contract stable — document changes in
  [docs/api.md](docs/api.md).
- **Don't** store API keys in plaintext (they're hashed: SHA-256 for lookup +
  bcrypt for proof-of-possession).
- **Don't** commit secrets or a populated `.env`.
- **Don't** take the service down for a non-critical dependency: the cache
  fails through to the DB, rate limiting fails open, and analytics degrade to
  synchronous. Preserve those degradation paths.
