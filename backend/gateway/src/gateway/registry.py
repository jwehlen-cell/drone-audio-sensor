from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog
from google.cloud import firestore

from .config import settings

log = structlog.get_logger(__name__)


@dataclass
class DeviceRegistration:
    device_id: str
    session_id: str
    first_seen_ms: int
    handshake_ms: int


class DeviceRegistry:
    """Backed by Firestore; one document per device under settings.devices_collection."""

    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client or firestore.AsyncClient(
            project=settings.gcp_project_id or None,
            database=settings.firestore_database,
        )
        self._collection = self._client.collection(settings.devices_collection)

    async def upsert_handshake(
        self,
        device_id: str,
        *,
        session_id: str,
        app_version: str,
        device_model: str,
        os_version: str,
        site_label: str,
        sample_rate_hz: int,
        frame_duration_ms: int,
        latitude: float | None,
        longitude: float | None,
        location_accuracy_m: float | None,
        location_status: str | None,
        auth_token_id: str,
    ) -> DeviceRegistration:
        now_ms = int(time.time() * 1000)
        doc = self._collection.document(device_id)
        snap = await doc.get()
        first_seen_ms = now_ms
        if snap.exists:
            data = snap.to_dict() or {}
            first_seen_ms = int(data.get("first_seen_ms", now_ms))

        payload: dict[str, Any] = {
            "device_id": device_id,
            "session_id": session_id,
            "first_seen_ms": first_seen_ms,
            "last_handshake_ms": now_ms,
            "last_seen_ms": now_ms,
            "app_version": app_version,
            "device_model": device_model,
            "os_version": os_version,
            "assigned_site_label": site_label,
            "sample_rate_hz": sample_rate_hz,
            "frame_duration_ms": frame_duration_ms,
            "auth_token_id": auth_token_id,
            "status": "active",
        }
        if latitude is not None and longitude is not None:
            payload["current_location"] = firestore.GeoPoint(latitude, longitude)
            payload["location_accuracy_m"] = location_accuracy_m
            payload["location_status"] = location_status
            payload["location_timestamp_ms"] = now_ms

        await doc.set(payload, merge=True)
        log.info(
            "device_handshake_persisted",
            device_id=device_id,
            session_id=session_id,
            site_label=site_label,
        )
        return DeviceRegistration(
            device_id=device_id,
            session_id=session_id,
            first_seen_ms=first_seen_ms,
            handshake_ms=now_ms,
        )

    async def update_location(
        self,
        device_id: str,
        latitude: float,
        longitude: float,
        accuracy_m: float | None,
        status: str | None,
        timestamp_ms: int,
    ) -> None:
        await self._collection.document(device_id).set(
            {
                "current_location": firestore.GeoPoint(latitude, longitude),
                "location_accuracy_m": accuracy_m,
                "location_status": status,
                "location_timestamp_ms": timestamp_ms,
            },
            merge=True,
        )

    async def mark_disconnected(self, device_id: str, *, reason: str) -> None:
        await self._collection.document(device_id).set(
            {
                "status": "offline",
                "last_disconnect_ms": int(time.time() * 1000),
                "last_disconnect_reason": reason,
            },
            merge=True,
        )

    async def close(self) -> None:
        # AsyncClient close is internal; nothing user-facing required.
        return None
