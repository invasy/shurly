#!/usr/bin/env bash
# Initialize the database: apply all Alembic migrations.
#
# Reads SHURLY_DATABASE_URL from the environment (or .env) so it targets the
# same database the app uses. `alembic/env.py` rewrites the postgres:// scheme
# to postgresql+asyncpg:// automatically.
set -euo pipefail

# Load a local .env if present (values do not override already-exported vars).
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${SHURLY_DATABASE_URL:-}" ]]; then
  echo "error: SHURLY_DATABASE_URL is not set (export it or add it to .env)" >&2
  exit 1
fi

echo "Applying migrations to: ${SHURLY_DATABASE_URL%%@*}@<redacted>"
alembic upgrade head

echo "done."
