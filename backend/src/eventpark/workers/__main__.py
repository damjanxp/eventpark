"""Run one worker: `python -m eventpark.workers entry|exit|notifications|reports|maintenance`.

Same image as the API, different command (ADR-001). On ECS each worker is its own service, scaled on its queue's
backlog; locally each is its own Compose service.
"""

import asyncio
import sys

import structlog

from eventpark.core.config import get_settings
from eventpark.core.logging import configure_logging
from eventpark.db.engine import create_engine
from eventpark.db.session import create_sessionmaker
from eventpark.messaging.relay import install_stop_signals
from eventpark.workers.consumer import Consumer, Handler
from eventpark.workers.maintenance import Maintenance
from eventpark.workers.notifications import Notifications
from eventpark.workers.reports import Reports
from eventpark.workers.stats import EntryStats, ExitStats

WORKERS: dict[str, type[Handler]] = {
    "entry": EntryStats,
    "exit": ExitStats,
    "notifications": Notifications,
    "reports": Reports,
    "maintenance": Maintenance,
}


async def main(kind: str) -> None:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings)
    handler = WORKERS[kind](create_sessionmaker(engine), settings)
    stop = asyncio.Event()
    install_stop_signals(stop)
    try:
        await Consumer(handler).run(stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in WORKERS:
        print(f"usage: python -m eventpark.workers {{{'|'.join(WORKERS)}}}", file=sys.stderr)
        sys.exit(2)
    structlog.contextvars.bind_contextvars(worker=sys.argv[1])
    asyncio.run(main(sys.argv[1]))
