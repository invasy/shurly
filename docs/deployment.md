# Deployment

`shurly` has three deployment targets, in increasing order of ceremony:
bare-metal / VM, **Docker Compose** (recommended for a single node), and
**Kubernetes** via the bundled Helm chart (recommended for clusters). All three
share the same image and the same configuration surface (environment
variables), so a deployment is a config difference, not a code difference.

---

## 1. Configuration

Everything is driven by environment variables prefixed `SHURLY_` (see
[`.env.example`](../.env.example) for the full list with defaults). The
variables that matter in production:

| Variable | Required | Notes |
| --- | --- | --- |
| `SHURLY_ENVIRONMENT` | — | Set to `production` to disable `/docs`/`/redoc`. |
| `SHURLY_DATABASE_URL` | **yes** | `postgresql://user:pass@host:5432/db`. `env.py` rewrites the scheme to `+asyncpg` automatically. |
| `SHURLY_REDIS_URL` | **yes** | `redis://host:6379/0`. |
| `SHURLY_BASE_URL` | **yes** | Public URL used to build `short_url` values. Must be what clients actually hit. |
| `SHURLY_ADMIN_TOKEN` | **yes** in prod | Guards list/revoke key endpoints. Leave empty only in development. |
| `SHURLY_ALLOW_LOCALHOST` | — | Defaults `true`; set `false` in production to reject loopback/private link targets. |
| `SHURLY_API_KEYS_ENABLED` | — | `true` by default. Set `false` for an open (unauthenticated) deployment — not recommended. |
| `SHURLY_SENTRY_DSN` | no | Enables error tracking if present. |
| `SHURLY_GEOIP_MMDB` | no | Path to a GeoLite2-Country.mmdb for country enrichment. |

---

## 2. Docker Compose (single node)

The simplest full stack — PostgreSQL, Redis, a one-shot migration job, the API,
a Celery worker, and a beat scheduler:

```bash
cp .env.example .env      # set SHURLY_DATABASE_URL/REDIS_URL if they live elsewhere
docker compose up --build
```

Services and their roles:

| Service | Image / command | Purpose |
| --- | --- | --- |
| `postgres` | `postgres:16-alpine` | system of record; volume `pgdata` |
| `redis` | `redis:7-alpine` | cache + rate-limit + Celery broker |
| `migrate` | app image | `alembic upgrade head` (runs once, gates `api`) |
| `api` | app image | `uvicorn app.main:app`, port `8000` |
| `worker` | app image | Celery worker (analytics persistence) |
| `beat` | app image | Celery beat (periodic rollup / GeoIP enrichment) |

Healthchecks are wired so `api` starts only after a successful migration and a
healthy Redis. To tear down: `docker compose down` (add `-v` to drop the DB).

The [Dockerfile](../Dockerfile) is multistage: a `uv`-based builder produces a
wheel installed into a venv, and the runtime stage is `python:3.12-slim`
running as the unprivileged `shurly` user, carrying only the venv plus the
Alembic tree. The default `CMD` runs the API; `migrate`, `worker`, and `beat`
override the command in `compose.yml`.

---

## 3. Kubernetes (Helm)

The chart lives in [`helm/`](../helm/). It deploys the `api` Deployment, a
`worker` Deployment, a `beat` Deployment (pinned to 1 replica — it schedules
periodic tasks), and a `Service` for the API. Schema migrations run as an
**initContainer** on the API pods (`alembic upgrade head`), which gates the
API on a ready schema and is idempotent, so no separate Job is needed.

```bash
helm install shurly ./helm \
  --namespace shurly --create-namespace \
  --set image.tag=v0.1.0 \
  --set env.SHURLY_BASE_URL=https://short.example.com \
  --set env.SHURLY_DATABASE_URL=postgresql://shurly:shurly@postgres.example.com:5432/shurly \
  --set env.SHURLY_REDIS_URL=redis://redis.example.com:6379/0 \
  --set env.SHURLY_ADMIN_TOKEN=$(openssl rand -base64 32)
```

Key `values.yaml` settings:

| Value | Default | Meaning |
| --- | --- | --- |
| `image.repository` / `image.tag` | `shurly` / `latest` | container image |
| `replicas.api` / `replicas.worker` | `2` / `2` | horizontal scale for each Deployment |
| `service.port` | `8000` | container port exposed by the Service |
| `env.*` | — | arbitrary `SHURLY_*` env passed to all workloads |
| `ingress.enabled` | `false` | enable to expose via an ingress controller |
| `migrate.enabled` | `true` | run the Alembic initContainer on API pods |
| `secretRef.*` | — | reference a Secret for `SHURLY_DATABASE_URL` / `SHURLY_ADMIN_TOKEN` |
| `resources.*` | — | CPU/memory requests/limits per workload |

The chart defines liveness on `/health` and readiness on `/ready` (see
[api.md](api.md#health--metrics)), so the load balancer only routes to ready
pods and recovers automatically when a dependency blips.

> **Secrets.** In a real cluster put `SHURLY_DATABASE_URL` and
> `SHURLY_ADMIN_TOKEN` in a `Secret` and reference them, rather than
> `--set`-ing them on the command line (which lands them in shell history).
> The chart accepts a `secretRef` for this; the inline form above is for
> quick experiments only.

---

## 4. Load testing

The redirect path is the one that matters for latency (target < 50ms on a
cache hit). Three ready-made approaches:

**hey** (quick single-box smoke test):

```bash
hey -n 100000 -c 500 http://localhost:8000/<an-existing-code>
```

Look at the `50%/99%` latencies and the error ratio; a healthy cache-hit
should be well under 50ms at the 99th percentile.

**locust** (distributed, ramped) — the project ships a `locust` dev dependency:

```bash
locust -f scripts/load_test.py \
  --host http://localhost:8000 \
  --users 2000 --spawn-rate 500 --headless -r 500
```

**k6** (if you prefer it):

```bash
k6 run scripts/load_test.js
```

A realistic mix to model: ~95% redirects (reads) and ~5% creates (writes),
which mirrors a real shortener's read/write ratio. Watch
`shurly_request_duration_seconds` and `shurly_redirects_total` on `/metrics`
while you ramp, and confirm the DB pool ( `SHURLY_DB_POOL_SIZE`) and Redis
connection pool don't saturate before the CPU does.
