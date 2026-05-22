from __future__ import annotations

import asyncio
import signal

import grpc
import structlog

import drone_audio_pb2_grpc as pb_grpc

from .config import settings
from .registry import DeviceRegistry
from .service import DroneAudioStreamServicer
from .state import DeviceStateStore

log = structlog.get_logger(__name__)


async def serve() -> None:
    registry = DeviceRegistry()
    state = DeviceStateStore()
    servicer = DroneAudioStreamServicer(registry, state)

    server = grpc.aio.server(
        options=[
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
            ("grpc.max_concurrent_streams", settings.grpc_max_concurrent_streams),
            ("grpc.max_send_message_length", 4 * 1024 * 1024),
            ("grpc.max_receive_message_length", 4 * 1024 * 1024),
        ],
    )
    pb_grpc.add_DroneAudioStreamServicer_to_server(servicer, server)

    bind_addr = f"{settings.grpc_host}:{settings.grpc_port}"
    server.add_insecure_port(bind_addr)
    await server.start()
    log.info("gateway_listening", bind=bind_addr)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()

    log.info("gateway_shutting_down")
    await server.stop(grace=10.0)
    await state.close()
    await registry.close()
    log.info("gateway_stopped")
