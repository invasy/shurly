# System design decisions

This document records the *why* behind the major architectural choices in
`shurly`. Each section states the decision, the alternatives considered, and
the trade-offs that drove the final call.

---

## 1. Code generation strategy

The core question: how do we turn a URL into a short, unique, URL-safe code
that (a) never collides, (b) is cheap to generate at 1K+ writes/s, and (c)
doesn't leak usage volume?

Two strategies are implemented, selected at runtime via `SHURLY_CODE_STRATEGY`.

### 1a. Base62 over a database sequence (default)

Codes are the base62 encoding of a monotonically increasing `BIGINT` drawn from
a PostgreSQL sequence (`nextval`). Base62 uses `[0-9A-Za-z]` (62 symbols), so
a 7-character code encodes `62^7 ≈ 3.5×10^12` distinct values — orders of
magnitude more than a `BIGINT` can ever hand out before it wraps (it won't,
at realistic volumes).

* **Pros**
  * **Zero collisions by construction.** The sequence is the single source of
    truth; two URLs can never produce the same integer, hence never the same
    code. No retry loop is needed on the happy path.
  * **O(1), allocation-free encode** — a small loop over base62 digits.
  * **Deterministic and short** — codes grow predictably as the sequence grows.
* **Cons**
  * **Sequential and guessable.** Codes are enumerable, so a client can walk
    `/0`, `/1`, … and discover other users' links. Fine for a general-purpose
    shortener (bit.ly is equally enumerable), wrong for confidential links.
  * **Leakage.** The sequence position encodes *when* a link was created,
    trivially exposing creation ordering and rough total volume.

### 1b. Murmur3 hash (opt-in)

Codes are derived by hashing `(url, user_id, salt)` with Murmur3 and mapping
the digest into the base62 alphabet at a target width (`SHURLY_CODE_LENGTH`).
On the (rare) unique-index violation we perturb the input and retry.

* **Pros**
  * **Non-enumerable** — there is no walking order; adjacent hashes are
    unrelated.
  * **Privacy** — a code reveals nothing about *when* the link was made.
  * **Deterministic per (url, user)** — this is what makes *idempotency*
    natural: the same input re-derives the same candidate code.
* **Cons**
  * **Collisions are possible** (birthday bound at the target width) and must
    be resolved by retry-against-the-unique-index. At `code_length=7` the
    collision probability stays far below 1% for billions of rows, so retries
    are effectively never taken.
  * A single extra DB round-trip *only* on the collision path.

### The trade-off in one line

Use **Base62** when links are public and volume/throughput is the priority
(default); use **Murmur3** when links are confidential and enumeration must be
prevented. Both share the same persistence layer, so switching is a config
change, not a rewrite.

> We deliberately do *not* use raw `uuid4` as the short code: 36 hex chars are
> too long for a shortener, and their randomness buys nothing over a salted
> hash. We also do *not* use a fixed-width Snowflake id as the code directly,
> because its time+machine bits are enumerable; Murmur3 scrubs those away.

---

## 2. Storage: PostgreSQL

The `urls` table is the system of record. PostgreSQL was chosen over MongoDB /
a KV store for the write path because:

* **ACID + a unique index on `short_code`** give collision-freedom *enforced by
  the database*, not by application logic that can race. This is the single
  most important guarantee for a shortener and it is structural here.
* **`BIGINT` sequences** make Base62 codes trivially cheap (see §1).
* **JSONB** leaves room for optional metadata (OpenGraph, preview) without
  schema churn.
* **Dialect portability.** The models use SQLAlchemy variant types (`Uuid`, a
  `BigIntPK`, `INET`-as-`String(45)`) so the *same* ORM code runs against
  PostgreSQL in production and **SQLite in tests** — which is what lets the
  test suite be hermetic (no containers, no network).

Indexes on `short_code` (unique), `user_id`, and `expires_at` cover the three
hot access paths (resolve, per-owner listing, expiration sweeps).

---

## 3. Caching: Redis, cache-aside, with a circuit breaker

Redirects are the read-heavy hot path (the spec targets 10K+ redirects/s,
sub-50ms). The strategy:

1. **Read from Redis first** (`short_code → {original_url, expires_at}`). A hit
   is a single sub-millisecond round-trip and skips the DB entirely.
2. **Miss → read DB → write back** to Redis with a TTL (`SHURLY_REDIS_CACHE_TTL`,
   default 24h).
3. **Invalidate on delete / expiration** — purge the key so a stale short code
   never resolves to a deleted URL.

This is classic **cache-aside**: the application, not a proxy, owns cache
coherency, which keeps the write path simple (write DB, delete cache).

### Circuit breaker

Redis is a dependency we *degrade gracefully* around. A small breaker wraps
every cache call: after a configurable number of consecutive failures it opens
and short-circuits subsequent calls for a cooldown window, **failing fast**
instead of letting every request pile up on a slow/dead socket. While open,
reads fall straight through to the DB — the service stays *up* (availability),
just slower. This is what keeps the 99.9% target alive during a Redis blip.

### Rate limiting in the same store

Per-key rate limiting is a **sliding window** implemented as a Lua script so
the "read window members → drop expired → append → count" sequence is atomic
under Redis's single-threaded model. No cross-process coordination needed, and
it scales horizontally because the state lives in Redis, not a worker process.
When no API key is present (or keys are disabled) the window is keyed by
client IP as a fallback.

---

## 4. Analytics: fire-and-forget, off the hot path

A click must not add latency to a redirect. The design:

* On a redirect the service **enqueues** a `record_click` job and returns the
  `307` *before* the click is persisted. With `SHURLY_USE_CELERY=true` this is a
  `PUBLISH` to the Celery/Redis broker (a network write, not a DB write); with
  `false` it degrades to a synchronous in-process record (used in tests and
  single-node dev).
* The **Celery worker** persists the event (referrer, user-agent, IP, and — if
  a GeoLite2 mmdb is configured — the ISO country) into the `analytics` table.
* A periodic **rollup** task maintains `urls.click_count` so the stats endpoint
  reads a counter, not an aggregate query.

The consequence: a burst of clicks can momentarily lag persistence, but the
redirect path's latency is bounded by one broker write. Analytics are
*at-least-once* — a dropped event loses a data point but never a redirect.

---

## 5. Auth: API keys, hashed in two stages

Keys are never stored in plaintext. We store **both**:

* a **SHA-256 digest** of the key (fast, indexed) used for *lookup* — at most
  one row, so this is a cheap unique-index probe, and
* a **bcrypt hash** of the key used for *proof-of-possession* verification.

SHA-256 alone is reversible under a dictionary attack and bcrypt alone is too
slow for a per-request lookup; storing both gets the speed of a digest and the
resistance of an adaptive hash. The plaintext is returned **exactly once** at
creation and is unrecoverable afterwards — by design.

`/api/v1/keys` generation is open (bootstrap the first key), while **list and
revoke** require the `X-Admin-Token` header to match `SHURLY_ADMIN_TOKEN`.
(When no admin token is configured the routes are open — a development
convenience that must be closed in production.)

---

## 6. Redirect semantics

* **`307 Temporary Redirect`** is the default so the request method and body
  are preserved by redirecting clients (safe for non-GET uses, matches the
  spec).
* **`301 Moved Permanently`** is opt-in via the `X-Stable: true` request
  header, for long-lived links where aggressive caching is desirable.
* An **expired** short code returns **`410 Gone`** (not `404`) — the resource
  *existed* and is deliberately gone, which is the semantically correct status
  and what clients are expected to cache aggressively.

---

## 7. Resilience & observability

* **Graceful degradation** across all three dependencies: Redis down → DB only
  (circuit breaker); Celery down → sync analytics; DB down → `/ready` fails,
  `/health` reports `degraded`, and redirect reads that were already cached
  keep serving.
* **Structured JSON logging** (structlog) with a **correlation id** injected by
  middleware and threaded through every log line and every error response
  (`X-Correlation-Id`), so a request is traceable end-to-end.
* **Prometheus** `/metrics`: request count, a latency histogram (with
  low-cardinality path bucketing), redirects served, and URLs created.
* **Security headers** on every response (`nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`).
