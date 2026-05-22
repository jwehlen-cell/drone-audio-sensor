from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

import redis.asyncio as redis_async
import structlog

from .config import settings
from .model import FrameScore

log = structlog.get_logger(__name__)


@dataclass
class FrameInput:
    device_id: str
    session_id: str
    sequence: int
    capture_timestamp_ms: int
    sample_rate_hz: int
    site_label: str
    pcm16_mono: bytes


@dataclass
class DetectionEvent:
    detection_id: str
    device_id: str
    session_id: str
    first_frame_sequence: int
    last_frame_sequence: int
    first_frame_timestamp_ms: int
    last_frame_timestamp_ms: int
    average_score: float
    peak_score: float
    frames_over_threshold: int
    window_frames: int
    threshold: float
    site_label: str
    model_name: str
    model_version: str
    class_scores: dict[str, float]


class DetectionState:
    """Per-device score ring buffer + suppression window, persisted in Redis."""

    def __init__(self, client: redis_async.Redis | None = None) -> None:
        self._client = client or redis_async.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
            decode_responses=True,
        )

    @staticmethod
    def _scores_key(device_id: str) -> str:
        return f"scores:{device_id}"

    @staticmethod
    def _suppression_key(device_id: str) -> str:
        return f"alert_suppression:{device_id}"

    async def append_score(
        self,
        device_id: str,
        *,
        sequence: int,
        timestamp_ms: int,
        drone_score: float,
        auxiliary_score: float,
    ) -> list[dict]:
        key = self._scores_key(device_id)
        entry = json.dumps(
            {
                "seq": sequence,
                "ts": timestamp_ms,
                "drone": drone_score,
                "aux": auxiliary_score,
            }
        )
        async with self._client.pipeline(transaction=True) as pipe:
            await pipe.lpush(key, entry)
            await pipe.ltrim(key, 0, settings.score_buffer_size - 1)
            await pipe.expire(key, settings.redis_ttl_seconds)
            await pipe.lrange(key, 0, -1)
            _, _, _, raw = await pipe.execute()
        return [json.loads(r) for r in raw]

    async def is_suppressed(self, device_id: str) -> bool:
        return bool(await self._client.exists(self._suppression_key(device_id)))

    async def mark_suppressed(self, device_id: str, detection_id: str) -> None:
        await self._client.set(
            self._suppression_key(device_id),
            detection_id,
            ex=settings.suppression_window_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()


def evaluate(buffer: list[dict]) -> tuple[bool, float, float, int]:
    """Return (should_trigger, avg_score, peak_score, frames_over_threshold)."""
    if not buffer:
        return False, 0.0, 0.0, 0
    scores = [float(b["drone"]) for b in buffer]
    over = sum(1 for s in scores if s >= settings.detection_threshold)
    avg = sum(scores) / len(scores)
    peak = max(scores)
    trigger = (
        len(buffer) >= settings.min_frames_over_threshold
        and over >= settings.min_frames_over_threshold
    )
    return trigger, avg, peak, over


def build_detection(
    *,
    frame: FrameInput,
    buffer: list[dict],
    score: FrameScore,
    avg: float,
    peak: float,
    over: int,
) -> DetectionEvent:
    first = buffer[-1]
    last = buffer[0]
    return DetectionEvent(
        detection_id=uuid.uuid4().hex,
        device_id=frame.device_id,
        session_id=frame.session_id,
        first_frame_sequence=int(first["seq"]),
        last_frame_sequence=int(last["seq"]),
        first_frame_timestamp_ms=int(first["ts"]),
        last_frame_timestamp_ms=int(last["ts"]),
        average_score=avg,
        peak_score=peak,
        frames_over_threshold=over,
        window_frames=len(buffer),
        threshold=settings.detection_threshold,
        site_label=frame.site_label,
        model_name=settings.model_name,
        model_version=settings.model_version,
        class_scores=score.class_scores,
    )


def now_ms() -> int:
    return int(time.time() * 1000)
