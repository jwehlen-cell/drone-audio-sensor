from __future__ import annotations

import asyncio

from .logging_setup import configure_logging
from .server import serve


def main() -> None:
    try:
        import uvloop  # type: ignore[import-not-found]

        uvloop.install()
    except Exception:  # noqa: BLE001
        pass

    configure_logging()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
