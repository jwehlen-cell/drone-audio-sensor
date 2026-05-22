from __future__ import annotations

import uvicorn

from .config import settings
from .logging_setup import configure_logging
from .server import build_app


def main() -> None:
    configure_logging()
    app = build_app()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
