from __future__ import annotations

import asyncio
import signal

import structlog

from .health_server import HealthServer
from .logging_setup import configure_logging
from .worker import InferenceWorker

log = structlog.get_logger(__name__)


async def run() -> None:
    worker = InferenceWorker()
    health = HealthServer(ready_check=lambda: worker.ready)
    await health.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    worker_task = asyncio.create_task(worker.start())
    stop_task = asyncio.create_task(stop_event.wait())

    done, _pending = await asyncio.wait(
        {worker_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    log.info("shutting_down")
    if stop_task in done:
        worker_task.cancel()
    await worker.stop()
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
