from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

import structlog
from google.cloud import pubsub_v1

from .config import settings

log = structlog.get_logger(__name__)


@dataclass
class IncomingMessage:
    detection_id: str
    device_id: str
    body: dict
    ack_fn: Callable[[], None] = field(repr=False)
    nack_fn: Callable[[], None] = field(repr=False)

    def ack(self) -> None:
        try:
            self.ack_fn()
        except Exception:  # noqa: BLE001
            pass

    def nack(self) -> None:
        try:
            self.nack_fn()
        except Exception:  # noqa: BLE001
            pass


class _LruSet:
    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._store: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> bool:
        if key in self._store:
            self._store.move_to_end(key)
            return False
        self._store[key] = None
        if len(self._store) > self._cap:
            self._store.popitem(last=False)
        return True


class DetectionSubscriber:
    """Bridges google-cloud-pubsub streaming pull into an asyncio queue."""

    def __init__(self) -> None:
        self._client = pubsub_v1.SubscriberClient()
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=512)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._future = None
        self._dedup = _LruSet(settings.dedup_cache_size)

    def start(self) -> None:
        if not settings.detections_subscription:
            raise RuntimeError("TAK_PUBLISHER_DETECTIONS_SUBSCRIPTION is not set")
        self._loop = asyncio.get_running_loop()

        flow = pubsub_v1.types.FlowControl(max_messages=64, max_bytes=10 * 1024 * 1024)
        self._future = self._client.subscribe(
            settings.detections_subscription,
            callback=self._on_message,
            flow_control=flow,
        )
        log.info("subscription_started", subscription=settings.detections_subscription)

    def stop(self) -> None:
        if self._future is not None:
            self._future.cancel()
            try:
                self._future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        while True:
            msg = await self._queue.get()
            yield msg

    def _on_message(self, message) -> None:
        try:
            body = json.loads(message.data.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("detection_parse_failed", error=str(e))
            message.ack()
            return

        detection_id = (
            message.attributes.get("detection_id")
            or body.get("detection_id")
            or message.message_id
        )
        device_id = (
            message.attributes.get("device_id")
            or body.get("device_id")
            or "unknown"
        )

        if not self._dedup.add(detection_id):
            log.info("duplicate_detection_skipped", detection_id=detection_id)
            message.ack()
            return

        incoming = IncomingMessage(
            detection_id=detection_id,
            device_id=device_id,
            body=body,
            ack_fn=message.ack,
            nack_fn=message.nack,
        )

        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._queue.put(incoming), self._loop)
