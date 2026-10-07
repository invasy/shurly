# syntax=docker/dockerfile:1
#
# shurly -- multistage build.
#
#   stage 1 (builder): install `uv`, create a venv, build the project wheel
#                      and install it (runtime deps only).
#   stage 2 (runtime): slim image carrying just the final venv and the
#                      Alembic migration tree. The `app` package ships inside
#                      site-packages, so `uvicorn app.main:app` and the
#                      `alembic`/`celery` CLIs all resolve from the venv.

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Fast, self-contained dependency resolver/ installer.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy everything needed to build the wheel (hatchling reads pyproject + app/).
COPY pyproject.toml README.md LICENSE ./
COPY app ./app

# Build and install the wheel (runtime dependencies only) into the venv.
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache-dir .

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    SHURLY_HOST=0.0.0.0 \
    SHURLY_PORT=8000

# Non-root user for the service.
RUN useradd --create-home --shell /usr/sbin/nologin shurly

WORKDIR /app

# The venv already contains the installed `app` package (from the wheel).
COPY --from=builder /opt/venv /opt/venv
# Alembic needs its script tree + ini at the workdir for the `migrate` job.
COPY --from=builder /app/alembic ./alembic
COPY --from=builder /app/alembic.ini ./alembic.ini

USER shurly

EXPOSE 8000

# Default: run the API. compose.yml overrides the command for `migrate`,
# `worker`, and `beat`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
