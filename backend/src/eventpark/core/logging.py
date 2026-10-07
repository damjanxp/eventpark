"""Structured logging with structlog.

Every log line is one JSON object on stdout, e.g.
    {"ts": "2026-10-07T18:00:00.123Z", "level": "info", "msg": "app.started", "service": "api"}
On AWS, ECS ships stdout to CloudWatch Logs, where these fields become searchable.

Two kinds of loggers write here:
- our code: `structlog.get_logger()` -> processed by the structlog chain below;
- libraries (uvicorn, later SQLAlchemy, boto3) use Python's stdlib `logging` -> routed through the same
  processors by `ProcessorFormatter`, so they come out as the same JSON instead of plain text.

Context fields (like `request_id`) are stored in contextvars and merged into every line logged while
handling that request.
"""

import logging
import sys
from typing import Any

import structlog

from eventpark.core.config import Settings


def _add_service(service_name: str) -> structlog.types.Processor:
    def processor(_logger: Any, _method: str, event_dict: structlog.types.EventDict) -> Any:
        event_dict.setdefault("service", service_name)
        return event_dict

    return processor


def configure_logging(settings: Settings) -> None:
    # Steps applied to every log line, from our code and from libraries alike.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,  # request_id etc. bound for this request
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _add_service(settings.service_name),
    ]

    final_processors: list[structlog.types.Processor]
    if settings.log_format == "json":
        final_processors = [
            structlog.processors.format_exc_info,  # exception -> "exception" string field
            structlog.processors.EventRenamer("msg"),  # structlog calls the message "event"
            structlog.processors.JSONRenderer(),
        ]
    else:
        final_processors = [structlog.dev.ConsoleRenderer()]

    # Our own loggers: run the shared steps, then hand over to the stdlib formatter below.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # One stdout handler for everything; it renders both structlog and plain stdlib records.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *final_processors,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers; remove them so its lines go through ours (and aren't printed twice).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # The request-ID middleware logs one structured "request.finished" line per request instead.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
