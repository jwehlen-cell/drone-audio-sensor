from __future__ import annotations

import asyncio

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


class InferenceWorker:
    def __init__(self) -> None:
        self._model = YAMNetModel()
        self._consumer = FrameStreamConsumer()
        self._detection_state = DetectionState()
        self._publisher = DetectionPublisher()
        self._ready = False
        self._frames_processed = 0
        self._detections_emitted = 0

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        log.info("worker_starting")
        await asyncio.to_thread(self._model.load)
        self._ready = True
        log.info("worker_ready")
        await self._consumer.run(self._handle_frame)

    async def _handle_frame(self, frame: FrameInput) -> None:
        score = await asyncio.to_thread(
            self._model.infer_pcm16,
            frame.pcm16_mono,
            frame.sample_rate_hz,
        )
        buffer = await self._detection_state.append_score(
            frame.device_id,
            sequence=frame.sequence,
            timestamp_ms=frame.capture_timestamp_ms,
            drone_score=score.drone_score,
            auxiliary_score=score.auxiliary_score,
        )
        self._frames_processed += 1

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
        await self._detection_state.mark_suppressed(frame.device_id, event.detection_id)
        await self._publisher.publish(event)
        self._detections_emitted += 1

    async def stop(self) -> None:
        log.info("worker_stopping", frames=self._frames_processed, detections=self._detections_emitted)
        self._consumer.stop()
        await self._consumer.close()
        await self._detection_state.close()
        self._publisher.close()
