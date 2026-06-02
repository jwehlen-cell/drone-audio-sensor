from __future__ import annotations

from datetime import datetime, timezone
import os
import time
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from pydantic import BaseModel, Field
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
TEMPLATES.env.filters["utc_ms"] = lambda v: _format_utc_ms(v)
TEMPLATES.env.filters["phone_local_ms"] = (
    lambda v, lat, lon: _format_phone_local_ms(v, lat, lon)
)


class SimulatedPhone(BaseModel):
    device_id: str
    lat: float
    lon: float
    site_label: str = ""
    battery_percent: int = Field(default=90, ge=0, le=100)
    network_type: str = "wifi"


class SimulatedPhonesRequest(BaseModel):
    phones: list[SimulatedPhone]
    timestamp_ms: int | None = None
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    tick: int = Field(default=1, ge=0)


def _resolve_user(request: Request) -> str:
    """Identify the caller.

    Two modes, controlled by `settings.require_auth` (env: `ADMIN_REQUIRE_AUTH`):

      false (default, R&D mode)
          The IAM check is *not* enforced server-side. Whatever the
          `X-Goog-Authenticated-User-Email` header says is used for
          audit logging; if absent, the caller is labeled `anonymous`.
          Pair this with `admin_allow_unauthenticated_invocations =
          true` in Terraform so the Cloud Run service is reachable
          without an identity token.

      true (production)
          The header MUST be present (set by Cloud Run + IAM /
          IAP). Missing header → HTTP 401. Pair this with
          `admin_allow_unauthenticated_invocations = false`
          and grant `roles/run.invoker` only to specific principals
          via `admin_invoker_members`.

    The legacy `ADMIN_ALLOW_UNAUTHENTICATED=true` env var still works
    as a local-dev bypass when `require_auth=true`, so existing
    development scripts don't break.

    TODO: Replace this with an IAP-integrated session once we wire IAP
    in front of the admin Cloud Run service. The header-trust shortcut
    is fine for internal R&D but not a hardened production model.
    """
    email = request.headers.get("X-Goog-Authenticated-User-Email")
    if email:
        return email.replace("accounts.google.com:", "")

    if not settings.require_auth:
        # R&D mode: leave the auth plumbing in place but let unauthenticated
        # callers through. Audit-log entries will identify them as anonymous.
        return "anonymous"

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
        # The dot column is red iff the device has published a detection
        # within the last `recent_detection_red_seconds`. Built from the
        # already-fetched detection list — no extra Firestore round-trip.
        now_ms_int = int(time.time() * 1000)
        recent_cutoff_ms = now_ms_int - settings.recent_detection_red_seconds * 1000
        hot_devices = {
            d.device_id for d in detections
            if d.published_at_ms and d.published_at_ms >= recent_cutoff_ms
        }
        # Join live state with registered state.
        joined = []
        for l in live:
            registered = device_by_id.get(l.device_id)
            joined.append(
                {
                    "live": l,
                    "registered": registered,
                    "freshness_seconds": _freshness(l.last_seen_ms),
                    "has_recent_detection": l.device_id in hot_devices,
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

        # Auto-refresh interval: honor a `?refresh=` query override so an
        # operator can pause refreshes while inspecting a detection without
        # editing config. `refresh=off` (or 0) disables.
        refresh_param = request.query_params.get("refresh")
        if refresh_param is not None:
            try:
                refresh_seconds = 0 if refresh_param.lower() in {"off", "0", "false", "no"} \
                    else max(0, int(refresh_param))
            except ValueError:
                refresh_seconds = settings.status_refresh_seconds
        else:
            refresh_seconds = settings.status_refresh_seconds

        return TEMPLATES.TemplateResponse(
            "status.html",
            {
                "request": request,
                "user": user,
                "live_devices": joined,
                "detections": detections,
                "stale_warning_seconds": settings.stale_warning_seconds,
                "stale_offline_seconds": settings.stale_offline_seconds,
                "recent_detection_red_seconds": settings.recent_detection_red_seconds,
                "refresh_seconds": refresh_seconds,
                # Operator-facing refresh chips. Order matters — used as
                # the rendered order in the template.
                "refresh_options": [5, 10, 30, 60],
                "rendered_at_ms": now_ms_int,
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

    @app.post("/api/simulate/phones")
    async def api_simulate_phones(
        payload: SimulatedPhonesRequest,
        request: Request,
        user: str = Depends(_resolve_user),
        fs: FirestoreRepo = Depends(fs_dep),
        r: RedisRepo = Depends(redis_dep),
    ) -> Any:
        if not settings.simulator_enabled:
            raise HTTPException(404, "simulator endpoint disabled")
        if settings.simulator_token:
            supplied = request.headers.get("X-SOH-Simulator-Token", "")
            if supplied != settings.simulator_token:
                raise HTTPException(403, "invalid simulator token")
        if len(payload.phones) > 100:
            raise HTTPException(400, "too many simulated phones")

        import time

        timestamp_ms = payload.timestamp_ms or int(time.time() * 1000)
        phones = [p.model_dump() for p in payload.phones]
        await fs.upsert_simulated_devices(phones, timestamp_ms=timestamp_ms)
        await r.refresh_simulated_devices(
            phones,
            timestamp_ms=timestamp_ms,
            ttl_seconds=payload.ttl_seconds,
            tick=payload.tick,
        )
        log.info(
            "simulated_phones_refreshed",
            user=user,
            count=len(phones),
            ttl_seconds=payload.ttl_seconds,
        )
        return {"refreshed": len(phones), "timestamp_ms": timestamp_ms}

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


def _format_utc_ms(value: int | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (TypeError, ValueError, OSError):
        return "—"


# timezonefinder loads ~24 MB of polygon data on first use; keep a single
# module-level instance and lazy-init it so import time stays cheap on
# code paths that never render the status page.
_TZ_FINDER = None


def _get_timezone_finder():
    global _TZ_FINDER
    if _TZ_FINDER is None:
        from timezonefinder import TimezoneFinder
        _TZ_FINDER = TimezoneFinder()
    return _TZ_FINDER


def _format_phone_local_ms(
    value: int | None,
    lat: float | None,
    lon: float | None,
) -> str:
    if not value:
        return "—"
    try:
        ts = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "—"
    if lat is None or lon is None:
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        tz_name = _get_timezone_finder().timezone_at(lat=float(lat), lng=float(lon))
    except Exception:  # noqa: BLE001
        tz_name = None
    if not tz_name:
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:  # noqa: BLE001
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
