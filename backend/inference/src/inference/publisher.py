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
        device_fields = await self._lookup_device_fields(event.device_id)
        location = device_fields.get("location") if device_fields else None
        site = device_fields.get("site") if device_fields else None
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
            # Site grouping key (e.g. "Patrick", "Shaw"). The admin UI
            # filters every view by this; an event with no site is
            # invisible until backfilled. Inherited from the device's
            # registry doc, not the streaming handshake — sites are a
            # provisioning concern, not a per-stream one.
            "site": site or "",
            "site_label": event.site_label,
            "model": {
                "name": event.model_name,
                "version": event.model_version,
            },
            "class_scores": event.class_scores,
            "subtype": {
                "label": event.subtype_label,
                "confidence": event.subtype_confidence,
                "probs": event.subtype_probs,
            },
            "category": {
                # See model.py::categorize for definitions.
                "token": event.category,
                "display": event.category_display,
            },
            "device_location": location,
            "published_at_ms": now_ms(),
            # Temporal gate: True when the sensor is firing chronically (stationary
            # nuisance). Audited in Firestore but withheld from the operator/TAK
            # Pub/Sub channel below.
            "chronic_suppressed": event.chronic_suppressed,
            "chronic_recent_count": event.chronic_recent_count,
        }
        payload = json.dumps(message).encode("utf-8")

        # Observable suppression: a chronically-firing sensor's alert is written
        # to Firestore (audit / dashboard can filter on chronic_suppressed) but
        # NOT published to the operator/TAK topic. Nothing is silently dropped.
        if event.chronic_suppressed:
            log.info(
                "detection_chronic_suppressed",
                detection_id=event.detection_id,
                device_id=event.device_id,
                recent_fires=event.chronic_recent_count,
            )
            try:
                await self._write_detection_doc(event, message, message_id="")
            except Exception as e:  # noqa: BLE001
                log.warning("detection_doc_write_failed", detection_id=event.detection_id, error=str(e))
            return ""

        attributes = {
            "device_id": event.device_id,
            "detection_id": event.detection_id,
            "site": site or "",
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

    async def _lookup_device_fields(self, device_id: str) -> dict | None:
        """One Firestore round-trip that pulls everything the detection
        publisher needs from the device's registry doc: location and
        site grouping. Returns ``{"location": {...} | None, "site": str | None}``
        or None when the doc doesn't exist."""
        try:
            snap = await self._firestore.collection(settings.devices_collection).document(
                device_id
            ).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            site = data.get("site")
            loc = data.get("current_location")
            if loc is None:
                return {"location": None, "site": site}
            return {
                "location": self._location_from_doc(data, loc),
                "site": site,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("device_lookup_failed", device_id=device_id, error=str(e))
            return None

    @staticmethod
    def _location_from_doc(data: dict, loc) -> dict:
        return {
            "latitude": float(loc.latitude),
            "longitude": float(loc.longitude),
            "accuracy_m": data.get("location_accuracy_m"),
            "status": data.get("location_status"),
            "timestamp_ms": data.get("location_timestamp_ms"),
        }

    def close(self) -> None:
        try:
            self._publisher.transport.close()
        except Exception:  # noqa: BLE001
            pass
