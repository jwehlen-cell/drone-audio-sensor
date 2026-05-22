from __future__ import annotations

import json
from dataclasses import dataclass

import redis.asyncio as redis_async
import structlog

from .config import settings

log = structlog.get_logger(__name__)


@dataclass
class LiveDeviceState:
    device_id: str
    session_id: str
    site_label: str
    last_seen_ms: int
    last_sequence: int
    frames_received: int
    dropped_frames: int
    reconnect_count: int
    network_type: str
    battery_percent: int
    thermal_state: str
    app_version: str


class RedisRepo:
    def __init__(self, client: redis_async.Redis | None = None) -> None:
        self._client = client or redis_async.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
            decode_responses=True,
        )

    async def list_live_devices(self) -> list[LiveDeviceState]:
        pattern = f"{settings.device_state_key_prefix}*"
        rows: list[LiveDeviceState] = []
        async for key in self._client.scan_iter(match=pattern, count=128):
            raw = await self._client.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rows.append(_to_live(data))
        rows.sort(key=lambda r: r.device_id)
        return rows

    async def close(self) -> None:
        await self._client.aclose()


def _to_live(data: dict) -> LiveDeviceState:
    return LiveDeviceState(
        device_id=str(data.get("device_id") or ""),
        session_id=str(data.get("session_id") or ""),
        site_label=str(data.get("site_label") or ""),
        last_seen_ms=int(data.get("last_seen_ms") or 0),
        last_sequence=int(data.get("last_sequence") or -1),
        frames_received=int(data.get("frames_received") or 0),
        dropped_frames=int(data.get("dropped_frames") or 0),
        reconnect_count=int(data.get("reconnect_count") or 0),
        network_type=str(data.get("network_type") or "unspecified"),
        battery_percent=int(data.get("battery_percent") or -1),
        thermal_state=str(data.get("thermal_state") or "unspecified"),
        app_version=str(data.get("app_version") or ""),
    )
