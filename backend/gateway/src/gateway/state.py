from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

import redis.asyncio as redis_async
import structlog

from .config import settings

log = structlog.get_logger(__name__)


@dataclass
class DeviceState:
    device_id: str
    session_id: str
    last_seen_ms: int
    last_sequence: int
    frames_received: int
    dropped_frames: int
    reconnect_count: int
    app_version: str
    network_type: str
    battery_percent: int
    thermal_state: str
    site_label: str


class DeviceStateStore:
    """Per-device hot state in Redis; one key per device with a short TTL."""

    def __init__(self, client: redis_async.Redis | None = None) -> None:
        self._client = client or redis_async.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
            decode_responses=True,
        )

    @staticmethod
    def _key(device_id: str) -> str:
        return f"device:{device_id}"

    async def init_session(self, state: DeviceState) -> None:
        await self._client.set(
            self._key(state.device_id),
            json.dumps(asdict(state)),
            ex=settings.redis_ttl_seconds,
        )

    async def touch(
        self,
        device_id: str,
        *,
        sequence: int,
        frames_received: int,
    ) -> None:
        key = self._key(device_id)
        raw = await self._client.get(key)
        if raw is None:
            return
        data = json.loads(raw)
        data["last_seen_ms"] = int(time.time() * 1000)
        data["last_sequence"] = sequence
        data["frames_received"] = frames_received
        await self._client.set(key, json.dumps(data), ex=settings.redis_ttl_seconds)

    async def update_health(
        self,
        device_id: str,
        *,
        dropped_frames: int,
        reconnect_count: int,
        network_type: str,
        battery_percent: int,
        thermal_state: str,
    ) -> None:
        key = self._key(device_id)
        raw = await self._client.get(key)
        if raw is None:
            return
        data = json.loads(raw)
        data.update(
            dropped_frames=dropped_frames,
            reconnect_count=reconnect_count,
            network_type=network_type,
            battery_percent=battery_percent,
            thermal_state=thermal_state,
            last_seen_ms=int(time.time() * 1000),
        )
        await self._client.set(key, json.dumps(data), ex=settings.redis_ttl_seconds)

    async def clear(self, device_id: str) -> None:
        await self._client.delete(self._key(device_id))

    async def publish_frame(
        self,
        *,
        device_id: str,
        session_id: str,
        sequence: int,
        capture_timestamp_ms: int,
        sample_rate_hz: int,
        pcm16_mono: bytes,
        site_label: str,
        codec: str = "",
    ) -> None:
        # The gateway is a pass-through for the audio payload. It never
        # decodes; the inference worker handles ``codec`` before YAMNet.
        # Storing the compressed bytes here also shrinks the Redis stream
        # footprint linearly with the compression ratio.
        fields: dict[str, object] = {
            "d": device_id,
            "ses": session_id,
            "s": str(sequence),
            "t": str(capture_timestamp_ms),
            "r": str(sample_rate_hz),
            "l": site_label,
            "p": pcm16_mono,
        }
        if codec:
            fields["c"] = codec
        await self._client.xadd(
            settings.frame_stream_key,
            fields,
            maxlen=settings.frame_stream_maxlen,
            approximate=True,
        )

    async def close(self) -> None:
        await self._client.aclose()
