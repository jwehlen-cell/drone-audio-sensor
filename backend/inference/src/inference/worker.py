from __future__ import annotations

import asyncio
import os
import time

import structlog

from .config import settings
from .detection import (
    DetectionState,
    FrameInput,
    build_detection,
    evaluate,
)
from .model import YAMNetModel
from .publisher import DetectionPublisher
from .stream_consumer import FrameStreamConsumer

log = structlog.get_logger(__name__)


def _decode_payload(
    pcm16_mono: bytes, codec: str, frame_sample_rate_hz: int
) -> tuple[bytes, int]:
    """Lossless codec dispatch. Returns ``(pcm16_le_bytes, sample_rate_hz)``.

    Behavior per codec:
      ""/"pcm16" -> bytes returned unchanged; sample rate comes from
                    the Redis stream metadata (i.e. what the producer
                    said the frame was captured at).
      "flac"     -> libsndfile decodes FLAC; sample rate comes from the
                    FLAC stream's own STREAMINFO block (authoritative).
      "wav"      -> libsndfile decodes WAV (RIFF/PCM or any other PCM
                    subtype it can read); sample rate from the WAV
                    "fmt " chunk (authoritative).

    For wav/flac we trust the in-stream header over the frame metadata
    because the file is self-describing. Multichannel sources are
    downmixed to mono via simple averaging so YAMNet always sees a
    single channel.

    Any other codec name raises so we don't silently feed garbage to
    YAMNet.
    """
    if not codec or codec == "pcm16":
        return pcm16_mono, frame_sample_rate_hz
    if codec in ("flac", "wav"):
        import io
        import soundfile as sf  # local imports keep cold-start cost off
                                # the critical path for legacy raw-PCM frames
        audio, sr = sf.read(io.BytesIO(pcm16_mono), dtype="int16")
        if audio.ndim > 1:
            # libsndfile downmix safety: average across channels if the
            # producer sent multichannel (Android in some configs, or a
            # NiFi-supplied stereo WAV).
            audio = audio.mean(axis=1).astype("int16")
        return audio.tobytes(), int(sr)
    raise ValueError(f"unsupported audio codec: {codec!r}")


class InferenceWorker:
    def __init__(self) -> None:
        self._model = YAMNetModel()
        self._consumer = FrameStreamConsumer()
        self._detection_state = DetectionState()
        self._publisher = DetectionPublisher()
        self._ready = False
        self._frames_processed = 0
        self._detections_emitted = 0
        self._detections_chronic_muted = 0
        # Watchdog state. ``_last_frame_at`` is reset to ``time.monotonic()``
        # when the worker becomes ready and every time a frame is
        # processed. The watchdog loop wakes periodically and exits
        # the process if the gap exceeds the configured threshold.
        self._last_frame_at: float = 0.0
        self._watchdog_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        log.info("worker_starting")
        await asyncio.to_thread(self._model.load)
        self._ready = True
        log.info("worker_ready")
        self._last_frame_at = time.monotonic()
        if settings.watchdog_stall_seconds > 0:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        await self._consumer.run(self._handle_frame)

    async def _watchdog_loop(self) -> None:
        """Force-exit the process if no frame has been processed in
        ``settings.watchdog_stall_seconds``. Cloud Run will replace
        the container — which the HTTP ``/healthz`` probe cannot
        trigger because it's a separate thread that keeps returning
        200 even when the Redis stream consumer thread is blocked on
        a dropped socket.
        """
        stall_threshold = settings.watchdog_stall_seconds
        check_interval = settings.watchdog_check_interval_seconds or max(
            30, stall_threshold // 4
        )
        log.info(
            "worker_watchdog_started",
            stall_threshold_seconds=stall_threshold,
            check_interval_seconds=check_interval,
        )
        while True:
            await asyncio.sleep(check_interval)
            stall = time.monotonic() - self._last_frame_at
            if stall > stall_threshold:
                log.critical(
                    "worker_watchdog_stall",
                    stall_seconds=int(stall),
                    threshold_seconds=stall_threshold,
                    frames_processed=self._frames_processed,
                    detections_emitted=self._detections_emitted,
                )
                # Bypass the asyncio shutdown path — the worker thread
                # is presumed wedged, so an orderly ``stop()`` would
                # likely hang too. Cloud Run treats a non-zero exit
                # as a crash and spawns a replacement.
                os._exit(1)

    async def _handle_frame(self, frame: FrameInput) -> None:
        # decode_payload may override the sample rate when the codec
        # carries its own header (wav/flac); for raw PCM the frame
        # metadata is authoritative.
        pcm, sample_rate_hz = await asyncio.to_thread(
            _decode_payload,
            frame.pcm16_mono,
            frame.codec,
            frame.sample_rate_hz,
        )
        # Audio duration represented by this frame. The detection gate
        # uses this to convert "K of N frames over threshold" into
        # "≥ K seconds of evidence", so wide-cadence stations are not
        # silently filtered out.
        frame_seconds = (len(pcm) / 2.0) / float(sample_rate_hz)
        score = await asyncio.to_thread(
            self._model.infer_pcm16,
            pcm,
            sample_rate_hz,
        )
        buffer = await self._detection_state.append_score(
            frame.device_id,
            sequence=frame.sequence,
            timestamp_ms=frame.capture_timestamp_ms,
            drone_score=score.drone_score,
            auxiliary_score=score.auxiliary_score,
            frame_seconds=frame_seconds,
            confounder_score=score.confounder_score,
        )
        self._frames_processed += 1
        self._last_frame_at = time.monotonic()

        trigger, avg, peak, over = evaluate(buffer)
        if not trigger:
            return

        if await self._detection_state.is_suppressed(frame.device_id):
            log.debug("detection_suppressed", device_id=frame.device_id)
            return

        event = build_detection(
            frame=frame,
            buffer=buffer,
            score=score,
            avg=avg,
            peak=peak,
            over=over,
        )
        # Temporal gate (mechanism 1): a chronically-firing sensor is a
        # stationary nuisance source. Mute its OPERATOR alert (still audited in
        # Firestore via the publisher) but keep tracking it.
        chronic, count = await self._detection_state.record_fire_and_chronic(
            frame.device_id, frame.capture_timestamp_ms
        )
        event.chronic_suppressed = chronic
        event.chronic_recent_count = count
        await self._detection_state.mark_suppressed(frame.device_id, event.detection_id)
        await self._publisher.publish(event)
        if chronic:
            self._detections_chronic_muted += 1
            log.info("detection_chronic_muted", device_id=frame.device_id,
                     recent_fires=count, detection_id=event.detection_id)
        else:
            self._detections_emitted += 1

    async def stop(self) -> None:
        log.info("worker_stopping", frames=self._frames_processed, detections=self._detections_emitted)
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._consumer.stop()
        await self._consumer.close()
        await self._detection_state.close()
        self._publisher.close()
