# Shortcuts for everyday commands. `make` alone lists them.
# Each target is just the command(s) you'd otherwise type; read them to learn what really runs.

.DEFAULT_GOAL := help
.PHONY: help up down reset logs ps migrate seed test test-fast lint fmt smoke

BACKEND := cd backend &&
# Host-side tools (alembic, smoke script) read the same .env as Compose; its URLs point at localhost.
ENV_FILE := --env-file ../.env

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-10s %s\n", $$1, $$2}'

.env: ## (automatic) create .env from .env.example on a fresh clone
	cp .env.example .env

up: .env ## Build and start the whole local stack, wait until everything is healthy
	docker compose up -d --build --wait

down: ## Stop the stack (data in volumes is kept)
	docker compose down

reset: ## Stop the stack AND delete its volumes (all local data is gone)
	docker compose down -v

logs: ## Follow the logs of all services (Ctrl+C to stop)
	docker compose logs -f

ps: ## Show the stack's containers and their health
	docker compose ps -a

migrate: .env ## Apply database migrations from your machine (the init service does this on `make up`)
	$(BACKEND) uv run $(ENV_FILE) alembic upgrade head

seed: .env ## Demo venue, users (one per role), a live + a future event; device keys -> .devices.local.json
	$(BACKEND) uv run $(ENV_FILE) python -m eventpark.seed --out ../.devices.local.json

test: ## All backend tests (integration tests start their own containers: needs Docker)
	$(BACKEND) uv run pytest

test-fast: ## Backend tests that need no Docker
	$(BACKEND) uv run pytest -m "not integration"

lint: ## The same checks as CI: lint, formatting, types
	$(BACKEND) uv run ruff check . && uv run ruff format --check . && uv run mypy src
	uvx ruff check scripts && uvx ruff format --check scripts

fmt: ## Auto-fix lint issues and format the code
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .
	uvx ruff check --fix scripts && uvx ruff format scripts

smoke: .env ## Check the local S3/SQS emulators behave like AWS (stack must be running)
	uv run --env-file .env scripts/smoke_aws_emulators.py
