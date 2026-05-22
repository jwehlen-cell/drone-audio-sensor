from __future__ import annotations

import asyncio
import signal

import structlog

from .health_server import HealthServer
from .logging_setup import configure_logging
from .publisher import Publisher

log = structlog.get_logger(__name__)


async def run() -> None:
    publisher = Publisher()
    health = HealthServer(ready_check=lambda: publisher.ready)
    await health.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    publisher_task = asyncio.create_task(publisher.start())
    stop_task = asyncio.create_task(stop_event.wait())

    done, _pending = await asyncio.wait(
        {publisher_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    log.info("shutting_down")
    if stop_task in done:
        publisher_task.cancel()
    await publisher.stop()
    await health.stop()


def main() -> None:
    try:
        import uvloop  # type: ignore[import-not-found]

        uvloop.install()
    except Exception:  # noqa: BLE001
        pass
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
