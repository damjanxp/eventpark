"""Tests for the request-ID middleware (`eventpark.api.middleware`).

The contract:
1. If the request has an `X-Request-ID` header with a sane value, use it.
   "Sane" = 1-128 characters, only letters, digits, `.`, `_`, `-`.
2. Otherwise (missing, empty, too long, weird characters) generate a new one (a UUID4 string is fine).
3. Return the ID in the response header `X-Request-ID`.
4. Every log line written while handling the request contains `"request_id": "<the id>"`.
5. The ID must not leak into the next request (each request gets its own context).

These tests only look at HTTP behaviour and log output, not at how the middleware is built.
"""

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient

from eventpark.app import create_app
from eventpark.core.config import Settings


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Overrides the app fixture from conftest.py: same app plus one test-only route."""
    app = create_app(settings)

    # A test-only route that writes one log line, so we can check that it carries the request_id.
    @app.get("/_test/log")
    async def log_something() -> dict[str, str]:
        structlog.get_logger().info("test.hello")
        return {"ok": "yes"}

    return app


@pytest.fixture
def log_output(app: FastAPI) -> Iterator[io.StringIO]:
    """Everything the app logs, rendered exactly like stdout (same formatter), collected in a buffer."""
    root = logging.getLogger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(root.handlers[0].formatter)  # the JSON formatter set up by configure_logging
    root.addHandler(handler)
    yield buffer
    root.removeHandler(handler)


def _log_lines(buffer: io.StringIO, msg: str) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
    return [line for line in lines if line.get("msg") == msg]


async def test_generates_id_when_header_missing(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    request_id = response.headers.get("x-request-id")
    assert request_id, "response must carry an X-Request-ID header"


async def test_each_request_gets_a_different_generated_id(client: AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_reuses_incoming_id(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "gate-07.scan_123-abc"})

    assert response.headers["x-request-id"] == "gate-07.scan_123-abc"


@pytest.mark.parametrize(
    "bad_value",
    [
        "",  # empty
        "x" * 129,  # too long: someone could fill our logs with megabytes
        "abc def",  # space
        'abc", "level": "error',  # trying to inject fake fields into a log line
    ],
)
async def test_replaces_unsafe_incoming_id(client: AsyncClient, bad_value: str) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": bad_value})

    request_id = response.headers["x-request-id"]
    assert request_id
    assert request_id != bad_value


async def test_log_lines_during_request_carry_the_id(client: AsyncClient, log_output: io.StringIO) -> None:
    response = await client.get("/_test/log", headers={"X-Request-ID": "req-42"})
    assert response.status_code == 200

    lines = _log_lines(log_output, "test.hello")
    assert len(lines) == 1
    assert lines[0]["request_id"] == "req-42"


async def test_id_does_not_leak_into_next_request(client: AsyncClient, log_output: io.StringIO) -> None:
    await client.get("/_test/log", headers={"X-Request-ID": "first-request"})
    second = await client.get("/_test/log")

    lines = _log_lines(log_output, "test.hello")
    assert len(lines) == 2
    assert lines[0]["request_id"] == "first-request"
    assert lines[1]["request_id"] == second.headers["x-request-id"]
    assert lines[1]["request_id"] != "first-request"


async def test_logs_one_summary_line_per_request(client: AsyncClient, log_output: io.StringIO) -> None:
    response = await client.get("/_test/log", headers={"X-Request-ID": "req-7"})

    lines = _log_lines(log_output, "request.finished")
    assert len(lines) == 1
    line = lines[0]
    assert line["request_id"] == response.headers["x-request-id"] == "req-7"
    assert line["method"] == "GET"
    assert line["route"] == "/_test/log"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)
