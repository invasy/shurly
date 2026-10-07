# shurly documentation

| Document | Purpose |
| --- | --- |
| [design.md](design.md) | System design decisions and trade-offs (code generation, storage, caching, analytics) |
| [api.md](api.md) | HTTP API reference with request/response examples |
| [deployment.md](deployment.md) | Running locally, in Docker Compose, and on Kubernetes (Helm); load testing |

The OpenAPI schema is served by the running app at `/openapi.json` (and rendered
at `/docs` / `/redoc` outside of production). This documentation complements
rather than duplicates it: use the generated docs for exact field semantics and
these pages for the *why* and the operational runbook.
