from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import redis.asyncio as redis_async
import structlog

from .config import settings
from .model import FrameScore, categorize

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
    # Lossless codec applied to pcm16_mono before transmission. Empty
    # string and "pcm16" both mean raw PCM16 (legacy). "zstd" means the
    # bytes are zstandard-compressed PCM16 and must be decompressed
    # before feeding YAMNet.
    codec: str = ""


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
    subtype_label: str = ""
    subtype_confidence: float = 0.0
    subtype_probs: dict[str, float] = field(default_factory=dict)
    # Operational category derived from (peak_score, subtype_label):
    #   - "known_drone"   : trigger fired and characterizer matched a
    #                       trained subtype. ``category_display`` is the
    #                       raw subtype token (e.g. ``"mavicmini"``).
    #   - "unknown_drone" : trigger fired but characterizer said
    #                       no_drone. ``category_display`` = ``"Unknown
    #                       drone"``.
    # Always one of these two at detection time (detections only fire
    # above the trigger threshold).
    category: str = "known_drone"
    category_display: str = ""
    # Chronic-sensor mute: True when this sensor is firing chronically (a
    # stationary nuisance source). chronic_suppressed events are written to
    # Firestore for audit but NOT published to the operator/TAK channel.
    chronic_suppressed: bool = False
    chronic_recent_count: int = 0


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

    @staticmethod
    def _fires_key(device_id: str) -> str:
        return f"fires:{device_id}"

    async def record_fire_and_chronic(self, device_id: str, timestamp_ms: int) -> tuple[bool, int]:
        """Record a fired detection and report whether the sensor is chronic.

        Maintains a Redis sorted set of recent fire timestamps (trimmed to
        chronic_window_seconds). A sensor is chronic when it has accumulated
        >= chronic_alert_threshold fires in that trailing window -- the signature
        of a stationary nuisance source rather than a transient drone. Returns
        (is_chronic, count_in_window). Counts every fire (muted or not) so muting
        a chronic sensor doesn't lower its own count and cause oscillation.
        """
        if not settings.chronic_mute_enabled:
            return False, 0
        key = self._fires_key(device_id)
        window_ms = settings.chronic_window_seconds * 1000
        cutoff = timestamp_ms - window_ms
        async with self._client.pipeline(transaction=True) as pipe:
            await pipe.zadd(key, {str(timestamp_ms): timestamp_ms})
            await pipe.zremrangebyscore(key, 0, cutoff)
            await pipe.zcard(key)
            await pipe.expire(key, settings.chronic_window_seconds + settings.suppression_window_seconds)
            _, _, count, _ = await pipe.execute()
        return int(count) >= settings.chronic_alert_threshold, int(count)

    async def append_score(
        self,
        device_id: str,
        *,
        sequence: int,
        timestamp_ms: int,
        drone_score: float,
        auxiliary_score: float,
        frame_seconds: float,
        confounder_score: float = 0.0,
    ) -> list[dict]:
        key = self._scores_key(device_id)
        entry = json.dumps(
            {
                "seq": sequence,
                "ts": timestamp_ms,
                "drone": drone_score,
                "aux": auxiliary_score,
                # Audio duration represented by this frame, in seconds.
                # The evaluate() gate sums frame_s across positive frames
                # in the window — that's how a wide-cadence station's
                # single positive frame can still satisfy the gate.
                "frame_s": frame_seconds,
                # Max AudioSet confounder-class score for this frame; evaluate()
                # drops frames over the veto threshold (frog/insect/vehicle/train).
                "conf": confounder_score,
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
        # Clear the score buffer at the same time we mark suppression
        # so a wide-cadence station can't re-fire from stale positive
        # frames once the suppression window lifts. After this call, the
        # device starts accumulating fresh evidence from zero.
        async with self._client.pipeline(transaction=True) as pipe:
            await pipe.set(
                self._suppression_key(device_id),
                detection_id,
                ex=settings.suppression_window_seconds,
            )
            await pipe.delete(self._scores_key(device_id))
            await pipe.execute()

    async def close(self) -> None:
        await self._client.aclose()


def evaluate(buffer: list[dict]) -> tuple[bool, float, float, int]:
    """Return (should_trigger, avg_score, peak_score, frames_over_threshold).

    Trigger condition is seconds-of-audio-above-threshold across the
    window, not raw frame count, so a wide-cadence station whose
    single frame represents 30 s of audio can still fire on one
    positive detection. ``frames_over_threshold`` in the return tuple
    is retained for the downstream detection event so existing
    Firestore/Pub/Sub consumers see the same field semantics.
    """
    if not buffer:
        return False, 0.0, 0.0, 0
    scores = [float(b["drone"]) for b in buffer]
    over_frames = sum(1 for s in scores if s >= settings.detection_threshold)
    avg = sum(scores) / len(scores)
    peak = max(scores)

    def _counts(b: dict) -> bool:
        """A frame contributes to the gate iff it's over threshold AND not
        vetoed by a dominant AudioSet confounder (frog/insect/vehicle/train)."""
        if float(b["drone"]) < settings.detection_threshold:
            return False
        if (settings.confounder_veto_enabled
                and float(b.get("conf") or 0.0) >= settings.confounder_veto_threshold):
            return False
        return True

    seconds_over = sum(float(b.get("frame_s") or 0.0) for b in buffer if _counts(b))
    trigger = seconds_over >= settings.min_seconds_over_threshold
    return trigger, avg, peak, over_frames


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
    # Use peak_score for category so a single weak-subtype frame in the
    # buffer doesn't override an otherwise-strong detection. The current
    # frame's subtype_label is what the publisher carries downstream.
    category, category_display = categorize(peak, score.subtype_label)
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
        subtype_label=score.subtype_label,
        subtype_confidence=score.subtype_confidence,
        subtype_probs=score.subtype_probs,
        category=category,
        category_display=category_display,
    )


def now_ms() -> int:
    return int(time.time() * 1000)
