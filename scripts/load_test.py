"""Locust load test for shurly.

Models a realistic shortener load mix: ~95% redirect reads, ~5% URL creates.
Redirects are the latency-critical path, so they dominate.

Prerequisites
-------------
1. A running shurly (``docker compose up`` or a local uvicorn).
2. An API key (``SHURLY_API_KEY``) for the create flow.
3. A seed pool of existing short codes for the redirect flow, set via
   ``SHURLY_SEED_CODES`` (comma-separated). Create some up-front::

     curl -s -X POST "$SHURLY_BASE_URL/api/v1/urls" \
       -H "X-API-Key: $SHURLY_API_KEY" -H "Content-Type: application/json" \
       -d '{"url":"https://example.com/a"}'
     # repeat a few times, then collect the short_code values into
     # SHURLY_SEED_CODES="abc,def,ghi"

Run
---
    locust -f scripts/load_test.py \
        --host http://localhost:8000 \
        --users 2000 --spawn-rate 500 --headless -r 500
"""

from __future__ import annotations

import json
import os
import random
import time

from locust import FastHttpUser, task

_SEED = [c for c in os.environ.get("SHURLY_SEED_CODES", "seed1,seed2,seed3").split(",") if c]
_API_KEY = os.environ.get("SHURLY_API_KEY", "")


class ShurlyUser(FastHttpUser):
    wait_time = (0.05, 0.25)

    @task(10)
    def redirect(self) -> None:
        """95% of traffic: resolve a short code (the latency-critical hot path)."""
        if _SEED:
            self.client.get(f"/{_SEED[random.randrange(len(_SEED))]}")

    @task(1)
    def create(self) -> None:
        """5% of traffic: create a new short URL."""
        if not _API_KEY:
            return
        payload = json.dumps({"url": f"https://example.com/load/{time.monotonic_ns()}"})
        self.client.post(
            "/api/v1/urls",
            data=payload,
            headers={"X-API-Key": _API_KEY, "Content-Type": "application/json"},
        )
