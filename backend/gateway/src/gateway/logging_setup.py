import logging
import sys

import structlog

from .config import settings


def configure_logging() -> structlog.stdlib.BoundLogger:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    if settings.cloud_logging:
        try:
            from google.cloud.logging import Client
            from google.cloud.logging.handlers import StructuredLogHandler

            handler = StructuredLogHandler()
            root = logging.getLogger()
            root.handlers = [handler]
            root.setLevel(level)
            Client().setup_logging(log_level=level)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("Cloud Logging setup failed; falling back to stdout")

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.structured_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()
