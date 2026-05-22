from __future__ import annotations

import structlog

from .cot import build_event
from .subscriber import DetectionSubscriber, IncomingMessage
from .tak_client import TakClient

log = structlog.get_logger(__name__)


class Publisher:
    def __init__(self) -> None:
        self._tak = TakClient()
        self._subscriber = DetectionSubscriber()
        self._ready = False
        self._events_sent = 0
        self._events_dropped = 0

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        await self._tak.connect()
        self._subscriber.start()
        self._ready = True
        log.info("publisher_ready")

        async for message in self._subscriber.messages():
            await self._handle(message)

    async def _handle(self, message: IncomingMessage) -> None:
        event = build_event(message.body)
        if event is None:
            log.info(
                "detection_skipped_no_location",
                detection_id=message.detection_id,
                device_id=message.device_id,
            )
            self._events_dropped += 1
            message.ack()
            return

        try:
            await self._tak.send(event.to_xml_bytes())
            message.ack()
            self._events_sent += 1
            log.info(
                "cot_published",
                detection_id=message.detection_id,
                device_id=message.device_id,
                uid=event.uid,
                lat=event.lat,
                lon=event.lon,
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "cot_publish_failed",
                detection_id=message.detection_id,
                error=str(e),
            )
            message.nack()

    async def stop(self) -> None:
        log.info(
            "publisher_stopping",
            events_sent=self._events_sent,
            events_dropped=self._events_dropped,
        )
        self._subscriber.stop()
        await self._tak.close()
