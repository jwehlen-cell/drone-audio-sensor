from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import structlog
from google.cloud import firestore, pubsub_v1

from .config import settings
from .detection import DetectionEvent, now_ms

log = structlog.get_logger(__name__)


class DetectionPublisher:
    def __init__(
        self,
        publisher: pubsub_v1.PublisherClient | None = None,
        firestore_client: firestore.AsyncClient | None = None,
    ) -> None:
        self._publisher = publisher or pubsub_v1.PublisherClient()
        self._topic = settings.pubsub_detections_topic
        self._firestore = firestore_client or firestore.AsyncClient(
            project=settings.gcp_project_id or None,
            database=settings.firestore_database,
        )

    async def publish(self, event: DetectionEvent) -> str:
        location = await self._lookup_location(event.device_id)
        message = {
            "schema_version": 1,
            "detection_id": event.detection_id,
            "device_id": event.device_id,
            "session_id": event.session_id,
            "first_frame_sequence": event.first_frame_sequence,
            "last_frame_sequence": event.last_frame_sequence,
            "first_frame_timestamp_ms": event.first_frame_timestamp_ms,
            "last_frame_timestamp_ms": event.last_frame_timestamp_ms,
            "average_score": event.average_score,
            "peak_score": event.peak_score,
            "frames_over_threshold": event.frames_over_threshold,
            "window_frames": event.window_frames,
            "threshold": event.threshold,
            "site_label": event.site_label,
            "model": {
                "name": event.model_name,
                "version": event.model_version,
            },
            "class_scores": event.class_scores,
            "device_location": location,
            "published_at_ms": now_ms(),
        }
        payload = json.dumps(message).encode("utf-8")

        attributes = {
            "device_id": event.device_id,
            "detection_id": event.detection_id,
            "site_label": event.site_label or "",
            "model_name": event.model_name,
        }

        message_id = await asyncio.to_thread(
            self._publish_blocking, payload, attributes
        )
        log.info(
            "detection_published",
            detection_id=event.detection_id,
            device_id=event.device_id,
            message_id=message_id,
            score=event.average_score,
        )

        # Mirror to Firestore so the admin dashboard can show recent hits
        # without subscribing to Pub/Sub. The expires_at field is wired to
        # a Firestore TTL policy (Terraform google_firestore_field), so old
        # docs vanish automatically after detection_doc_ttl_seconds.
        try:
            await self._write_detection_doc(event, message, message_id)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "detection_doc_write_failed",
                detection_id=event.detection_id,
                error=str(e),
            )

        return message_id

    def _publish_blocking(self, payload: bytes, attributes: dict[str, str]) -> str:
        future = self._publisher.publish(self._topic, payload, **attributes)
        return future.result(timeout=30)

    async def _write_detection_doc(
        self,
        event: DetectionEvent,
        message: dict,
        message_id: str,
    ) -> None:
        created_at = datetime.now(tz=timezone.utc)
        expires_at = created_at + timedelta(seconds=settings.detection_doc_ttl_seconds)
        doc_payload = {
            **message,
            "pubsub_message_id": message_id,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        await self._firestore.collection(settings.detections_collection).document(
            event.detection_id
        ).set(doc_payload)

    async def _lookup_location(self, device_id: str) -> dict | None:
        try:
            snap = await self._firestore.collection(settings.devices_collection).document(
                device_id
            ).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            loc = data.get("current_location")
            if loc is None:
                return None
            return {
                "latitude": float(loc.latitude),
                "longitude": float(loc.longitude),
                "accuracy_m": data.get("location_accuracy_m"),
                "status": data.get("location_status"),
                "timestamp_ms": data.get("location_timestamp_ms"),
            }
        except Exception as e:  # noqa: BLE001
            log.warning("device_location_lookup_failed", device_id=device_id, error=str(e))
            return None

    def close(self) -> None:
        try:
            self._publisher.transport.close()
        except Exception:  # noqa: BLE001
            pass
