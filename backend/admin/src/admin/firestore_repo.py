from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog
from google.cloud import firestore

from .config import settings
from .state_machine import (
    InvalidTransitionError,
    STATE_WIPE_SENT,
    normalize,
    validate_admin_transition,
)

log = structlog.get_logger(__name__)


@dataclass
class DeviceRow:
    device_id: str
    state: str
    site_label: str
    app_version: str
    device_model: str
    os_version: str
    first_seen_ms: int | None
    last_seen_ms: int | None
    last_handshake_ms: int | None
    last_disconnect_ms: int | None
    wipe_requested_at_ms: int | None
    wipe_sent_at_ms: int | None
    last_wipe_session_id: str | None
    has_public_key: bool
    public_key_fingerprint: str
    location_lat: float | None
    location_lon: float | None
    location_accuracy_m: float | None
    location_timestamp_ms: int | None


@dataclass
class DetectionRow:
    detection_id: str
    device_id: str
    site_label: str
    average_score: float
    peak_score: float
    last_frame_timestamp_ms: int | None
    published_at_ms: int | None
    location_lat: float | None
    location_lon: float | None


class FirestoreRepo:
    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client or firestore.AsyncClient(
            project=settings.gcp_project_id or None,
            database=settings.firestore_database,
        )
        self._devices = self._client.collection(settings.devices_collection)
        self._detections = self._client.collection(settings.detections_collection)

    async def list_devices(self) -> list[DeviceRow]:
        rows: list[DeviceRow] = []
        async for snap in self._devices.stream():
            data = snap.to_dict() or {}
            rows.append(_to_device_row(snap.id, data))
        rows.sort(key=lambda r: r.device_id)
        return rows

    async def get_device(self, device_id: str) -> DeviceRow | None:
        snap = await self._devices.document(device_id).get()
        if not snap.exists:
            return None
        return _to_device_row(snap.id, snap.to_dict() or {})

    async def set_state(self, device_id: str, target_state: str) -> DeviceRow:
        """Atomically validate and write the new state.

        Uses an async transaction so a wipe_sent transition cannot race
        with an admin state change.
        """
        doc_ref = self._devices.document(device_id)

        @firestore.async_transactional
        async def _txn(txn: firestore.AsyncTransaction) -> dict[str, Any]:
            snap = await doc_ref.get(transaction=txn)
            if not snap.exists:
                raise InvalidTransitionError(f"unknown device: {device_id}")
            data = snap.to_dict() or {}
            current = normalize(data.get("state") or data.get("status"))
            validate_admin_transition(current, target_state)
            tgt = normalize(target_state)
            payload: dict[str, Any] = {"state": tgt}
            now_ms = int(time.time() * 1000)
            if tgt == "wipe_requested":
                payload["wipe_requested_at_ms"] = now_ms
            if tgt == "active":
                # Clear any wipe-request bookkeeping when reactivating.
                payload["wipe_requested_at_ms"] = None
            txn.set(doc_ref, payload, merge=True)
            return {**data, **payload, "device_id": device_id}

        txn = self._client.transaction()
        updated = await _txn(txn)
        log.info("device_state_changed", device_id=device_id, new_state=normalize(target_state))
        return _to_device_row(device_id, updated)

    async def list_recent_detections(self) -> list[DetectionRow]:
        cutoff_ms = int(time.time() * 1000) - settings.recent_detections_window_seconds * 1000
        # Avoid composite-index requirements: fetch the last N by
        # published_at_ms and filter the window in Python.
        query = (
            self._detections.order_by(
                "published_at_ms", direction=firestore.Query.DESCENDING
            )
            .limit(settings.recent_detections_limit)
        )
        rows: list[DetectionRow] = []
        async for snap in query.stream():
            data = snap.to_dict() or {}
            published = int(data.get("published_at_ms") or 0)
            if published and published < cutoff_ms:
                continue
            rows.append(_to_detection_row(snap.id, data))
        return rows

    async def upsert_simulated_devices(
        self,
        phones: list[dict[str, Any]],
        *,
        timestamp_ms: int,
    ) -> None:
        for phone in phones:
            device_id = str(phone["device_id"])
            await self._devices.document(device_id).set(
                {
                    "device_id": device_id,
                    "state": "active",
                    "assigned_site_label": str(phone.get("site_label") or ""),
                    "app_version": "sim-soh-1.0",
                    "device_model": "SOH simulator",
                    "os_version": "local-url",
                    "first_seen_ms": timestamp_ms,
                    "last_seen_ms": timestamp_ms,
                    "last_handshake_ms": timestamp_ms,
                    "current_location": firestore.GeoPoint(
                        float(phone["lat"]),
                        float(phone["lon"]),
                    ),
                    "location_accuracy_m": 5.0,
                    "location_status": "current",
                    "location_timestamp_ms": timestamp_ms,
                    "admin_notes": "Generated by scripts/simulate_soh_phones.py",
                },
                merge=True,
            )


def _to_device_row(device_id: str, data: dict[str, Any]) -> DeviceRow:
    state = normalize(data.get("state") or data.get("status"))
    public_key_pem = data.get("public_key_pem") or ""
    fingerprint = ""
    if isinstance(public_key_pem, str) and public_key_pem:
        # Last 12 chars of the trimmed PEM body — enough to identify a key
        # without leaking it.
        body = "".join(public_key_pem.strip().splitlines()[1:-1])
        fingerprint = "..." + body[-12:] if body else ""

    loc = data.get("current_location")
    lat = float(loc.latitude) if loc is not None else None
    lon = float(loc.longitude) if loc is not None else None

    return DeviceRow(
        device_id=device_id,
        state=state,
        site_label=str(data.get("assigned_site_label") or ""),
        app_version=str(data.get("app_version") or ""),
        device_model=str(data.get("device_model") or ""),
        os_version=str(data.get("os_version") or ""),
        first_seen_ms=_int(data.get("first_seen_ms")),
        last_seen_ms=_int(data.get("last_seen_ms")),
        last_handshake_ms=_int(data.get("last_handshake_ms")),
        last_disconnect_ms=_int(data.get("last_disconnect_ms")),
        wipe_requested_at_ms=_int(data.get("wipe_requested_at_ms")),
        wipe_sent_at_ms=_int(data.get("wipe_sent_at_ms")),
        last_wipe_session_id=(
            str(data.get("last_wipe_session_id"))
            if data.get("last_wipe_session_id")
            else None
        ),
        has_public_key=bool(public_key_pem),
        public_key_fingerprint=fingerprint,
        location_lat=lat,
        location_lon=lon,
        location_accuracy_m=_float(data.get("location_accuracy_m")),
        location_timestamp_ms=_int(data.get("location_timestamp_ms")),
    )


def _to_detection_row(detection_id: str, data: dict[str, Any]) -> DetectionRow:
    location = data.get("device_location") or {}
    return DetectionRow(
        detection_id=detection_id,
        device_id=str(data.get("device_id") or ""),
        site_label=str(data.get("site_label") or ""),
        average_score=float(data.get("average_score") or 0.0),
        peak_score=float(data.get("peak_score") or 0.0),
        last_frame_timestamp_ms=_int(data.get("last_frame_timestamp_ms")),
        published_at_ms=_int(data.get("published_at_ms")),
        location_lat=_float(location.get("latitude") if isinstance(location, dict) else None),
        location_lon=_float(location.get("longitude") if isinstance(location, dict) else None),
    )


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "DetectionRow",
    "DeviceRow",
    "FirestoreRepo",
    "InvalidTransitionError",
    "STATE_WIPE_SENT",
]
