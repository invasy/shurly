# shurly developer entry points.
#
# Every target is idempotent and uses the project's `uv` virtualenv. Run
# `make` (no args) to see this help.

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: sync
sync: ## Install/sync all dependencies (runtime + dev) into .venv
	uv sync --all-extras

.PHONY: dev
dev: ## Run the API with auto-reload (expects Postgres + Redis up)
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"

.PHONY: migrate
migrate: ## Apply Alembic migrations (reads SHURLY_DATABASE_URL / .env)
	uv run alembic upgrade head

.PHONY: revision
revision: ## Create a new empty migration (usage: make revision MSG="describe change")
	uv run alembic revision --autogenerate -m "$(MSG)"

.PHONY: test
test: ## Run the full pytest suite with coverage
	uv run pytest

.PHONY: test-cov
test-cov: ## Run tests and fail if branch coverage is below 80%
	uv run pytest --cov-fail-under=80

.PHONY: lint
lint: ## Ruff lint (auto-fixable applied)
	uv run ruff check --fix .

.PHONY: format
format: ## Ruff format
	uv run ruff format .

.PHONY: typecheck
typecheck: ## mypy (strict, per pyproject)
	uv run mypy

.PHONY: check
check: format lint typecheck test ## Format + lint + typecheck + test (CI parity)

.PHONY: up
up: ## Build and start the full Docker Compose stack
	docker compose up --build

.PHONY: down
down: ## Stop the Compose stack
	docker compose down

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage build/coverage htmlcov dist
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + || true
