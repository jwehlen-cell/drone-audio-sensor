from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import redis.asyncio as redis_async
import structlog

from .config import settings
from .detection import FrameInput

log = structlog.get_logger(__name__)

FrameHandler = Callable[[FrameInput], Awaitable[None]]


class FrameStreamConsumer:
    """Reads frames from a Redis Stream using a consumer group.

    Each instance joins the same group, so frames are partitioned across
    consumers and processed exactly once. Acked after the handler returns
    without raising.
    """

    def __init__(self, client: redis_async.Redis | None = None) -> None:
        self._client = client or redis_async.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
            decode_responses=False,
        )
        self._stop = asyncio.Event()
        self._group_ready = False

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._client.xgroup_create(
                name=settings.frame_stream_key,
                groupname=settings.consumer_group,
                id="0",
                mkstream=True,
            )
            log.info("consumer_group_created", group=settings.consumer_group)
        except redis_async.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        self._group_ready = True

    async def run(self, handler: FrameHandler) -> None:
        await self._ensure_group()
        consumer_name = f"{settings.consumer_name}-{id(self):x}"
        log.info(
            "stream_consumer_started",
            stream=settings.frame_stream_key,
            group=settings.consumer_group,
            consumer=consumer_name,
        )

        while not self._stop.is_set():
            try:
                resp = await self._client.xreadgroup(
                    groupname=settings.consumer_group,
                    consumername=consumer_name,
                    streams={settings.frame_stream_key: ">"},
                    count=settings.read_batch_size,
                    block=settings.read_block_ms,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("xreadgroup_failed", error=str(e))
                await asyncio.sleep(1.0)
                continue

            if not resp:
                continue

            for _stream, entries in resp:
                for entry_id, fields in entries:
                    frame = _decode_frame(fields)
                    if frame is None:
                        await self._client.xack(
                            settings.frame_stream_key,
                            settings.consumer_group,
                            entry_id,
                        )
                        continue
                    try:
                        await handler(frame)
                        await self._client.xack(
                            settings.frame_stream_key,
                            settings.consumer_group,
                            entry_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.exception(
                            "frame_handler_failed",
                            entry_id=entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
                            device_id=frame.device_id,
                            error=str(e),
                        )

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        await self._client.aclose()


def _decode_frame(fields: dict) -> FrameInput | None:
    def get_str(key: bytes) -> str:
        v = fields.get(key)
        return v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else (v or "")

    try:
        pcm = fields.get(b"p")
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) == 0:
            return None
        return FrameInput(
            device_id=get_str(b"d"),
            session_id=get_str(b"ses"),
            sequence=int(get_str(b"s") or "0"),
            capture_timestamp_ms=int(get_str(b"t") or "0"),
            sample_rate_hz=int(get_str(b"r") or "16000"),
            site_label=get_str(b"l"),
            pcm16_mono=bytes(pcm),
            # Field absent for legacy raw-PCM16 producers; codec="" tells
            # the worker to pass the bytes straight to YAMNet.
            codec=get_str(b"c"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("frame_decode_failed", error=str(e))
        return None
