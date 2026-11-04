"""`python -m eventpark.relay`: the outbox relay process (see messaging/relay.py)."""

import asyncio

from eventpark.core.config import get_settings
from eventpark.core.logging import configure_logging
from eventpark.db.engine import create_engine
from eventpark.db.session import create_sessionmaker
from eventpark.messaging.relay import SqsPublisher, install_stop_signals, run_relay


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings)
    stop = asyncio.Event()
    install_stop_signals(stop)
    try:
        await run_relay(create_sessionmaker(engine), SqsPublisher(settings), stop=stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
