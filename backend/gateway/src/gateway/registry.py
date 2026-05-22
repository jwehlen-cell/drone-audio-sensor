from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog
from google.cloud import firestore

from .config import settings
from .state_machine import (
    STATE_ACTIVE,
    STATE_SETUP_PENDING,
    STATE_WIPE_SENT,
    normalize,
)

log = structlog.get_logger(__name__)


@dataclass
class DeviceRegistration:
    device_id: str
    session_id: str
    first_seen_ms: int
    handshake_ms: int


@dataclass
class DeviceLookup:
    """Subset of the device doc needed on the hot auth path."""

    device_id: str
    state: str
    public_key_pem: str | None


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
        preserved_state: str | None = None
        if snap.exists:
            data = snap.to_dict() or {}
            first_seen_ms = int(data.get("first_seen_ms", now_ms))
            preserved_state = data.get("state")

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
        }
        # Only seed the state field if the device has no recorded state yet;
        # never clobber an existing lost/revoked/wipe_* state on handshake.
        if preserved_state is None:
            payload["state"] = STATE_ACTIVE
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
                "last_seen_ms": int(time.time() * 1000),
            },
            merge=True,
        )

    async def lookup(self, device_id: str) -> DeviceLookup | None:
        """Fetch the auth-relevant subset of a device document."""
        snap = await self._collection.document(device_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        pem = data.get("public_key_pem")
        return DeviceLookup(
            device_id=device_id,
            state=normalize(data.get("state") or data.get("status")),
            public_key_pem=pem if isinstance(pem, str) and pem else None,
        )

    async def get_public_key(self, device_id: str) -> str | None:
        """Backward-compatible accessor used by JwtAuth. Returns None for
        any state that should not be allowed to authenticate at all."""
        lookup = await self.lookup(device_id)
        if lookup is None:
            return None
        # Revoked and wipe_sent must not authenticate.
        # active / lost / wipe_requested all need a successful JWT verify so
        # the gateway can still take state-specific action.
        from .state_machine import is_connectable
        if not is_connectable(lookup.state):
            return None
        return lookup.public_key_pem

    async def get_state(self, device_id: str) -> str | None:
        lookup = await self.lookup(device_id)
        return lookup.state if lookup else None

    async def complete_setup(self, device_id: str, *, session_id: str) -> bool:
        """If the device is still setup_pending, atomically flip to active.

        Returns True iff this call did the flip — i.e. it was the first
        successful check-in from a freshly-provisioned device. Idempotent:
        a no-op if the device is already in any other state.
        """
        doc = self._collection.document(device_id)

        @firestore.async_transactional
        async def _txn(txn: firestore.AsyncTransaction) -> bool:
            snap = await doc.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            current = normalize(data.get("state") or data.get("status"))
            if current != STATE_SETUP_PENDING:
                return False
            txn.set(
                doc,
                {
                    "state": STATE_ACTIVE,
                    "setup_completed_at_ms": int(time.time() * 1000),
                    "setup_completed_session_id": session_id,
                },
                merge=True,
            )
            return True

        txn = self._client.transaction()
        return await _txn(txn)

    async def mark_wipe_sent(self, device_id: str, *, session_id: str) -> None:
        await self._collection.document(device_id).set(
            {
                "state": STATE_WIPE_SENT,
                "wipe_sent_at_ms": int(time.time() * 1000),
                "last_wipe_session_id": session_id,
            },
            merge=True,
        )

    async def mark_disconnected(self, device_id: str, *, reason: str) -> None:
        # We deliberately do NOT touch the state field here — that's the
        # lifecycle state. Connection state is derived from last_seen_ms
        # by the admin dashboard.
        await self._collection.document(device_id).set(
            {
                "last_disconnect_ms": int(time.time() * 1000),
                "last_disconnect_reason": reason,
            },
            merge=True,
        )

    async def close(self) -> None:
        # AsyncClient close is internal; nothing user-facing required.
        return None
