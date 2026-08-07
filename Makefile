.DEFAULT_GOAL := help
SHELL := /bin/bash

# Python is driven exclusively through uv, which pins the 3.12 interpreter from
# backend/.python-version. A bare `python`/`python3` here would pick up whatever
# is first on PATH — on a machine with conda that is the wrong interpreter.
# See docs/adr/0001-uv-as-python-toolchain.md.
# `uv run --project backend` leaves the working directory at the repo root,
# which puts pytest's testpaths and alembic.ini out of reach. Run from backend/.
UV      := uv
BACKEND := cd backend && $(UV) run
COMPOSE := docker compose
NPM     := npm --prefix web

.PHONY: help dev down logs test test-backend test-web lint lint-backend lint-web \
        format migrate downgrade revision seed install check

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

install: ## Install backend and web dependencies
	$(UV) sync --project backend
	$(NPM) install

# ---------------------------------------------------------------------------
# Local stack
# ---------------------------------------------------------------------------

dev: ## Bring up postgres, redis, mailpit, proxy, api, worker, web
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  proxy      http://localhost:8000/readyz"
	@echo "  api        http://localhost:8001/readyz"
	@echo "  web        http://localhost:5173"
	@echo "  mailpit    http://localhost:8025"
	@echo ""
	@echo "  next: make migrate"

down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

logs: ## Follow logs from all services
	$(COMPOSE) logs -f

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test: test-backend test-web ## Run the full suite (pytest + vitest)

test-backend: ## pytest — integration tests skip when postgres/redis are down
	$(BACKEND) pytest

bench: ## Latency benchmark against a seeded ledger (needs `make dev`)
	$(BACKEND) python scripts/seed.py --rows $(or $(rows),1000000) --rollup
	$(BACKEND) pytest -m perf -s


test-web: ## vitest
	$(NPM) run test -- --run

# ---------------------------------------------------------------------------
# Lint / types / format
# ---------------------------------------------------------------------------

lint: lint-backend lint-web ## ruff + mypy + eslint

lint-backend:
	$(BACKEND) ruff check .
	$(BACKEND) ruff format --check .
	$(BACKEND) mypy

lint-web:
	$(NPM) run lint

format: ## Apply ruff and prettier formatting
	$(BACKEND) ruff format .
	$(BACKEND) ruff check --fix .
	$(NPM) run format

check: lint test ## Everything CI runs

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

migrate: ## Apply migrations (needs `make dev` running)
	$(BACKEND) alembic upgrade head

downgrade: ## Roll back one migration — proves reversibility (§11.3)
	$(BACKEND) alembic downgrade -1

revision: ## Autogenerate a migration: make revision m="add users"
	@test -n "$(m)" || (echo 'usage: make revision m="describe the change"'; exit 1)
	$(BACKEND) alembic revision --autogenerate -m "$(m)"

seed: ## Demo user + synthetic ledger history (rows=N to change volume)
	$(BACKEND) python scripts/seed.py --rows $(or $(rows),50000)
