# syntax=docker/dockerfile:1
#
# One image for every backend role (ADR-001): api, outbox-relay, workers. They differ only by the command
# they start with (docker-compose.yml locally, the ECS task definition on AWS).
#
# Build:  docker build -t eventpark-backend ./backend
#
# Two stages ("multi-stage build"):
#   builder: has uv and installs the third-party dependencies into a virtual environment at /app/.venv
#   runtime: a fresh slim Python that receives that .venv plus our source code; uv and caches never ship

# Same base image in both stages, so the compiled packages in .venv match the Python that runs them.
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie

# ---------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

# Copy the uv binary from its official image (pinned to the version we use locally) instead of pip-installing it.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# UV_COMPILE_BYTECODE: pre-compile .py -> .pyc now, so containers start faster.
# UV_LINK_MODE=copy:  real files in .venv (the uv cache below is a separate mount, links into it would break).
# UV_PYTHON_DOWNLOADS=never: use the image's Python, never download another one.

WORKDIR /app

# Third-party dependencies only. This layer depends on nothing but pyproject.toml + uv.lock, so Docker reuses
# it from cache until the dependencies change. Changing our own code never re-runs it.
# --mount=type=bind: the two files are visible during this RUN only (not copied into a layer).
# --mount=type=cache: uv's download cache survives between builds on this machine, but isn't in the image.
# --no-install-project: install the dependencies, not eventpark itself (our code is copied separately below).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# A dedicated unprivileged user. If someone breaks into the app, they are "app", not root.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# Layer order = least to most frequently changing:
#   1. .venv  (~100 MB, changes when dependencies change)  -> usually reused from cache
#   2. src    (~100 KB, changes on every commit)           -> the only layer rebuilt and pushed to ECR
# No --chown: files stay owned by root, so the app user can read and run its code but not modify it.
COPY --from=builder /app/.venv /app/.venv
COPY src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# PYTHONPATH: Python finds the `eventpark` package in /app/src (it isn't pip-installed into .venv).
# PYTHONUNBUFFERED: write log lines immediately (otherwise they sit in a buffer and appear late, or get lost on a crash).

WORKDIR /app
USER app
EXPOSE 8000

# Liveness for plain `docker run` / Compose: hit /healthz (not /readyz, see api/health.py).
# Uses Python because slim images have no curl. ECS ignores this and uses the task definition's healthCheck,
# and non-HTTP roles (workers, relay) override it in docker-compose.yml.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# Exec form (a JSON list, no shell): uvicorn becomes PID 1 and receives SIGTERM directly when Docker/ECS
# stops the container, so it can finish in-flight requests and shut down cleanly.
# One process per container: on AWS we scale by running more tasks, not more workers inside one.
CMD ["uvicorn", "eventpark.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
