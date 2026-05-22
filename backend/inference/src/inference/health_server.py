from __future__ import annotations

import asyncio
from typing import Callable

import structlog
from aiohttp import web

from .config import settings

log = structlog.get_logger(__name__)


class HealthServer:
    def __init__(self, ready_check: Callable[[], bool]) -> None:
        self._ready_check = ready_check
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/livez", self._livez)
        app.router.add_get("/readyz", self._readyz)
        app.router.add_get("/", self._livez)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=settings.health_host,
            port=settings.health_port,
        )
        await self._site.start()
        log.info("health_server_started", port=settings.health_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _livez(self, _req: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _readyz(self, _req: web.Request) -> web.Response:
        ready = self._ready_check()
        if ready:
            return web.Response(text="ready")
        return web.Response(text="not ready", status=503)
