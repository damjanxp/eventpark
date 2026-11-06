# Shortcuts for everyday commands. `make` alone lists them.
# Each target is just the command(s) you'd otherwise type; read them to learn what really runs.

.DEFAULT_GOAL := help
.PHONY: help up up-scale demo-scale web openapi sim down reset logs ps migrate seed lane test test-fast lint fmt smoke

BACKEND := cd backend &&
# Host-side tools (alembic, smoke script) read the same .env as Compose; its URLs point at localhost.
ENV_FILE := --env-file ../.env

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-10s %s\n", $$1, $$2}'

.env: ## (automatic) create .env from .env.example on a fresh clone
	cp .env.example .env

up: .env ## Build and start the whole local stack, wait until everything is healthy
	docker compose up -d --build --wait

up-scale: .env ## Like `up`, plus a 2nd API instance (:8001) and a Caddy load balancer (:8080)
	docker compose --profile scale up -d --build --wait

demo-scale: ## After up-scale + seed: prove a socket on instance A gets updates caused via instance B
	uv run scripts/demo_two_instances.py

down: ## Stop the stack (data in volumes is kept)
	docker compose --profile scale down

reset: ## Stop the stack AND delete its volumes (all local data is gone)
	docker compose --profile scale down -v

logs: ## Follow the logs of all services (Ctrl+C to stop)
	docker compose logs -f

ps: ## Show the stack's containers and their health
	docker compose ps -a

migrate: .env ## Apply database migrations from your machine (the init service does this on `make up`)
	$(BACKEND) uv run $(ENV_FILE) alembic upgrade head

seed: .env ## Demo venue, users (one per role), a live + a future event; device keys -> .devices.local.json
	$(BACKEND) uv run $(ENV_FILE) python -m eventpark.seed --out ../.devices.local.json

GATE ?= N1
lane: ## Drive one lane by keyboard: make lane GATE=N1 (entry N1..S2, exit X1..X3); after `make seed`
	python3 scripts/run_lane.py $(GATE)

web: ## Run the frontend dev server on http://localhost:5173 (stack must be running)
	cd frontend && npm install --no-audit --no-fund && npm run dev

openapi: ## Regenerate the frontend's typed API client after changing the API
	$(BACKEND) uv run python -m eventpark.openapi_dump ../frontend/openapi.json
	cd frontend && npm run gen:api

SCENARIO ?= smoke
sim: .env ## Run a traffic scenario + invariant checks: make sim SCENARIO=rush-2000-in-15 (after make seed)
	cd simulator && uv run --env-file ../.env python -m simulator run scenarios/$(SCENARIO).yaml

test: ## All tests: backend (integration tests start containers: needs Docker) + gate controller
	$(BACKEND) uv run pytest
	cd gate-controller && uv run pytest
	cd fakepay && uv run pytest
	cd simulator && uv run pytest
	cd frontend && npm test

test-fast: ## Backend tests that need no Docker
	$(BACKEND) uv run pytest -m "not integration"

lint: ## The same checks as CI: lint, formatting, types
	$(BACKEND) uv run ruff check . && uv run ruff format --check . && uv run mypy src
	uvx ruff check scripts && uvx ruff format --check scripts
	cd gate-controller && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd fakepay && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd simulator && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd frontend && npm run typecheck && npm run lint

fmt: ## Auto-fix lint issues and format the code
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .
	uvx ruff check --fix scripts && uvx ruff format scripts

smoke: .env ## Check the local S3/SQS emulators behave like AWS (stack must be running)
	uv run --env-file .env scripts/smoke_aws_emulators.py
