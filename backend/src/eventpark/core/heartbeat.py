"""Liveness for processes without an HTTP port (workers, outbox relay, ticker). See ADR-026.

The API proves it is alive by answering GET /healthz. A worker has no port, so it proves it instead by *touching a
file* every time its main loop goes round. The container healthcheck runs

    python -m eventpark.core.heartbeat

which exits 0 if the file was touched within HEARTBEAT_MAX_AGE seconds (default 90) and 1 otherwise. That catches the
two failures "is the process running?" cannot: a loop stuck forever (deadlock, a hung network call without timeout)
and a loop that crashed in a background task while PID 1 lives on.

Docker Compose only *reports* unhealthy (and `up --wait` fails on it); ECS replaces the task (Phase 12).
"""

import os
import sys
import time
from pathlib import Path

DEFAULT_FILE = "/tmp/eventpark-heartbeat"  # noqa: S108 - per-container tmp, not shared
DEFAULT_MAX_AGE = 90.0


def _path() -> Path:
    return Path(os.environ.get("HEARTBEAT_FILE", DEFAULT_FILE))


def beat() -> None:
    """Call once per loop iteration. Cheap (one utime/mtime update); never raises."""
    try:
        _path().touch()
    except OSError:
        pass  # a failing heartbeat must not take the worker down; the healthcheck will report it


def age_seconds() -> float | None:
    try:
        return time.time() - _path().stat().st_mtime
    except FileNotFoundError:
        return None


def is_alive(max_age: float | None = None) -> bool:
    limit = max_age if max_age is not None else float(os.environ.get("HEARTBEAT_MAX_AGE", DEFAULT_MAX_AGE))
    age = age_seconds()
    return age is not None and age <= limit


if __name__ == "__main__":
    age = age_seconds()
    if is_alive():
        sys.exit(0)
    print("no heartbeat yet" if age is None else f"last heartbeat {age:.0f}s ago", file=sys.stderr)
    sys.exit(1)
