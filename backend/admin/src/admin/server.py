from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .firestore_repo import FirestoreRepo, InvalidTransitionError
from .redis_repo import RedisRepo
from .state_machine import (
    ALL_STATES,
    EXTRA_CONFIRM,
    allowed_next_states,
    normalize,
    requires_extra_confirmation,
)

log = structlog.get_logger(__name__)


BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _resolve_user(request: Request) -> str:
    """Identify the caller.

    Production deployments front this service with Cloud Run IAM
    (`run.invoker` required) — the load balancer / Cloud Run then sets
    `X-Goog-Authenticated-User-Email`. For local development we accept
    the unauthenticated path only when ADMIN_ALLOW_UNAUTHENTICATED=true.

    TODO: Replace this with an IAP-integrated session once we wire IAP in
    front of the admin Cloud Run service. The placeholder here is the
    minimum that keeps the service from being usable without an opt-in
    env knob.
    """
    email = request.headers.get("X-Goog-Authenticated-User-Email")
    if email:
        return email.replace("accounts.google.com:", "")
    if os.environ.get("ADMIN_ALLOW_UNAUTHENTICATED", "").lower() == "true":
        return "local-dev"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin UI requires authenticated Cloud Run invocation",
    )


def build_app() -> FastAPI:
    app = FastAPI(title="Drone Sensor Admin", docs_url=None, redoc_url=None)

    # Static assets (CSS/JS) — small, served from the same container.
    app.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="static",
    )

    firestore_repo = FirestoreRepo()
    redis_repo = RedisRepo()

    def fs_dep() -> FirestoreRepo:
        return firestore_repo

    def redis_dep() -> RedisRepo:
        return redis_repo

    # ------- Pages -------

    @app.get("/", response_class=HTMLResponse)
    async def status_page(
        request: Request,
        user: str = Depends(_resolve_user),
        fs: FirestoreRepo = Depends(fs_dep),
        r: RedisRepo = Depends(redis_dep),
    ) -> Any:
        devices = await fs.list_devices()
        live = await r.list_live_devices()
        detections = await fs.list_recent_detections()
        device_by_id = {d.device_id: d for d in devices}
        # Join live state with registered state.
        joined = []
        for l in live:
            registered = device_by_id.get(l.device_id)
            joined.append(
                {
                    "live": l,
                    "registered": registered,
                    "freshness_seconds": _freshness(l.last_seen_ms),
                }
            )
        # Pre-bake the map payload here so the template stays JSON-ignorant.
        # Only devices that have a known location end up on the map.
        map_phones = []
        for row in joined:
            registered = row["registered"]
            if registered is None or registered.location_lat is None:
                continue
            map_phones.append(
                {
                    "device_id": registered.device_id,
                    "lat": registered.location_lat,
                    "lon": registered.location_lon,
                    "state": registered.state,
                    "site": registered.site_label or "",
                    "freshness_seconds": row["freshness_seconds"],
                    "last_seen_ms": row["live"].last_seen_ms,
                    "battery_percent": row["live"].battery_percent,
                    "network_type": row["live"].network_type,
                }
            )
        map_detections = []
        for d in detections:
            if d.location_lat is None:
                continue
            map_detections.append(
                {
                    "detection_id": d.detection_id,
                    "device_id": d.device_id,
                    "lat": d.location_lat,
                    "lon": d.location_lon,
                    "average_score": d.average_score,
                    "peak_score": d.peak_score,
                    "site": d.site_label or "",
                    "published_at_ms": d.published_at_ms,
                }
            )

        return TEMPLATES.TemplateResponse(
            "status.html",
            {
                "request": request,
                "user": user,
                "live_devices": joined,
                "detections": detections,
                "stale_warning_seconds": settings.stale_warning_seconds,
                "stale_offline_seconds": settings.stale_offline_seconds,
                "map_data": {
                    "phones": map_phones,
                    "detections": map_detections,
                    "stale_warning_seconds": settings.stale_warning_seconds,
                    "stale_offline_seconds": settings.stale_offline_seconds,
                },
            },
        )

    @app.get("/registered", response_class=HTMLResponse)
    async def registered_page(
        request: Request,
        user: str = Depends(_resolve_user),
        fs: FirestoreRepo = Depends(fs_dep),
    ) -> Any:
        devices = await fs.list_devices()
        rows = []
        for d in devices:
            allowed = sorted(allowed_next_states(d.state))
            rows.append(
                {
                    "device": d,
                    "allowed_transitions": allowed,
                    "extra_confirm": {
                        t: requires_extra_confirmation(d.state, t) for t in allowed
                    },
                }
            )
        return TEMPLATES.TemplateResponse(
            "registered.html",
            {
                "request": request,
                "user": user,
                "rows": rows,
                "all_states": sorted(ALL_STATES),
            },
        )

    # ------- API -------

    @app.post("/api/devices/{device_id}/state")
    async def change_state(
        device_id: str,
        target: str = Form(...),
        confirm: str = Form(""),
        user: str = Depends(_resolve_user),
        fs: FirestoreRepo = Depends(fs_dep),
    ) -> Any:
        current_device = await fs.get_device(device_id)
        if current_device is None:
            raise HTTPException(404, f"unknown device: {device_id}")
        target_norm = normalize(target)
        if requires_extra_confirmation(current_device.state, target_norm) and confirm != "yes":
            raise HTTPException(400, "transition requires extra confirmation")
        try:
            updated = await fs.set_state(device_id, target_norm)
        except InvalidTransitionError as e:
            raise HTTPException(400, str(e))
        log.info(
            "admin_state_change",
            user=user,
            device_id=device_id,
            from_state=current_device.state,
            to_state=updated.state,
        )
        return RedirectResponse("/registered", status_code=303)

    @app.get("/api/connected")
    async def api_connected(
        user: str = Depends(_resolve_user),
        r: RedisRepo = Depends(redis_dep),
    ) -> Any:
        live = await r.list_live_devices()
        return JSONResponse(
            [
                {
                    "device_id": l.device_id,
                    "session_id": l.session_id,
                    "site_label": l.site_label,
                    "last_seen_ms": l.last_seen_ms,
                    "frames_received": l.frames_received,
                    "dropped_frames": l.dropped_frames,
                    "reconnect_count": l.reconnect_count,
                    "network_type": l.network_type,
                    "battery_percent": l.battery_percent,
                    "thermal_state": l.thermal_state,
                    "app_version": l.app_version,
                }
                for l in live
            ]
        )

    @app.get("/api/registered")
    async def api_registered(
        user: str = Depends(_resolve_user),
        fs: FirestoreRepo = Depends(fs_dep),
    ) -> Any:
        devices = await fs.list_devices()
        return JSONResponse(
            [
                {
                    "device_id": d.device_id,
                    "state": d.state,
                    "site_label": d.site_label,
                    "last_seen_ms": d.last_seen_ms,
                    "has_public_key": d.has_public_key,
                    "public_key_fingerprint": d.public_key_fingerprint,
                    "wipe_requested_at_ms": d.wipe_requested_at_ms,
                    "wipe_sent_at_ms": d.wipe_sent_at_ms,
                    "location": (
                        {
                            "lat": d.location_lat,
                            "lon": d.location_lon,
                            "accuracy_m": d.location_accuracy_m,
                            "timestamp_ms": d.location_timestamp_ms,
                        }
                        if d.location_lat is not None
                        else None
                    ),
                }
                for d in devices
            ]
        )

    @app.get("/api/detections/recent")
    async def api_detections_recent(
        user: str = Depends(_resolve_user),
        fs: FirestoreRepo = Depends(fs_dep),
    ) -> Any:
        rows = await fs.list_recent_detections()
        return JSONResponse(
            [
                {
                    "detection_id": d.detection_id,
                    "device_id": d.device_id,
                    "site_label": d.site_label,
                    "average_score": d.average_score,
                    "peak_score": d.peak_score,
                    "last_frame_timestamp_ms": d.last_frame_timestamp_ms,
                    "published_at_ms": d.published_at_ms,
                    "location": (
                        {"lat": d.location_lat, "lon": d.location_lon}
                        if d.location_lat is not None
                        else None
                    ),
                }
                for d in rows
            ]
        )

    @app.get("/livez")
    async def livez() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict:
        # Cheap readiness — we don't ping Firestore on every check.
        return {"status": "ready"}

    return app


def _freshness(last_seen_ms: int) -> float | None:
    """Seconds since last_seen, or None if no last_seen recorded."""
    import time

    if not last_seen_ms:
        return None
    return max(0.0, time.time() - (last_seen_ms / 1000))
