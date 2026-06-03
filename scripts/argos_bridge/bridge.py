#!/usr/bin/env python3
"""Argos UAT pull-based replay bridge.

For each of 33 simulated SH-* stations, list ~N hours of historical
clips from the prod Argos GCS bucket (read-only), resample them from
the source 8 kHz to the 16 kHz the YAMNet inference worker requires,
and stream them through the local DroneAudioStream gateway in real
time. When a station's queue is exhausted, the source is re-listed
and the loop starts over.

Location is resolved PER CLIP (not per station) with this order:

    1. PRIMARY  — sidecar JSON next to the .wav.
                  ``location.{latitude, longitude, altitude}`` from
                  the same-basename .json blob in GCS.
    2. FALLBACK — BigQuery ``argos-487318.argos.sensor_locations``,
                  one query at startup, cached for the bridge's
                  lifetime. (No altitude column; defaults to 0.0.)

The handshake carries the FIRST clip's resolved location; subsequent
clips whose resolved location differs from the last sent value
generate a LocationUpdate before their AudioFrame, so the stream
reflects per-clip movement.

Sanity guard: when a sidecar GPS is >50 km from the station's
registry position we emit a WARNING but still use the sidecar value.
Set ``BRIDGE_SNAP_TO_REGISTRY=true`` to snap-to-registry instead.

Auth: ``GATEWAY_REQUIRE_AUTH`` is OFF in argosuat, so the bridge
sends device_id in the handshake but does not sign a JWT. The PKI
material minted by ``mint_test_pki.py`` and the public keys enrolled
by ``enroll_stations.py`` exist so we can flip ``require_auth`` later
without re-touching every station.

Env vars (all optional, defaults shown):
  BRIDGE_GATEWAY_URL        drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app
  BRIDGE_GATEWAY_TLS        true
  BRIDGE_GCS_BUCKET         aftac-argos-dataflow-unzipped
  BRIDGE_GCS_PREFIX         ensco/SH
  BRIDGE_WINDOW_HOURS       4
  BRIDGE_CODEC              pcm16            (or "flac")
  BRIDGE_REFRESH_INTERVAL_S 1800             (re-list GCS every 30 min)
  BRIDGE_STATIONS           SH000,SH002,...  (subset for smoke test; default = all 33)
  BRIDGE_REGISTRY_TABLE     argos-487318.argos.sensor_locations
  BRIDGE_REGISTRY_SR_COL    sensor           (registry column holding station id)
  BRIDGE_SNAP_TO_REGISTRY   false
  BRIDGE_REGISTRY_REQUIRED  false            (true: refuse to start if BQ load fails)
  GOOGLE_CLOUD_PROJECT      argosuat
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import grpc
import grpc.aio
import numpy as np
import soundfile as sf
import scipy.signal as ss
from google.cloud import bigquery, storage

# Local imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations import STATIONS, BY_ID, short_id

# Reach the generated proto. The VM provisioner runs grpcio-tools to
# regenerate these into scripts/, same as replay_fleet.py expects.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import drone_audio_pb2 as pb  # type: ignore
import drone_audio_pb2_grpc as pb_grpc  # type: ignore

log = logging.getLogger("argos-bridge")

GATEWAY_URL = os.environ.get(
    "BRIDGE_GATEWAY_URL",
    "drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app",
)
GATEWAY_TLS = os.environ.get("BRIDGE_GATEWAY_TLS", "true").lower() in {"true", "1", "yes"}
GCS_BUCKET = os.environ.get("BRIDGE_GCS_BUCKET", "aftac-argos-dataflow-unzipped")
GCS_PREFIX = os.environ.get("BRIDGE_GCS_PREFIX", "ensco/SH")
WINDOW_HOURS = int(os.environ.get("BRIDGE_WINDOW_HOURS", "4"))
CODEC = os.environ.get("BRIDGE_CODEC", "pcm16")
REFRESH_INTERVAL_S = int(os.environ.get("BRIDGE_REFRESH_INTERVAL_S", "1800"))
STATIONS_ENV = os.environ.get("BRIDGE_STATIONS", "").strip()
REGISTRY_TABLE = os.environ.get(
    "BRIDGE_REGISTRY_TABLE", "argos-487318.argos.sensor_locations"
)
REGISTRY_SR_COL = os.environ.get("BRIDGE_REGISTRY_SR_COL", "sensor")
SNAP_TO_REGISTRY = os.environ.get("BRIDGE_SNAP_TO_REGISTRY", "false").lower() in {
    "true", "1", "yes"
}
REGISTRY_REQUIRED = os.environ.get("BRIDGE_REGISTRY_REQUIRED", "false").lower() in {
    "true", "1", "yes"
}

SOURCE_SR = 8000      # GCS clips are 8 kHz
TARGET_SR = 16000     # YAMNet requires 16 kHz; bridge resamples on the way in
CLIP_SECONDS = 4.096  # nominal clip duration; used for inter-clip pacing
SITE_LABEL = "SH"     # operator spec: every SH-* station's Site column = "SH"
SANITY_DIST_KM = 50.0
RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)


# ---------------------------------------------------------------------------
# Location resolver: sidecar -> BigQuery registry, with sanity check
# ---------------------------------------------------------------------------

@dataclass
class ResolvedLocation:
    latitude: float
    longitude: float
    altitude_m: float
    timestamp_ms: int
    provider: str  # "sidecar-gps" or "registry"
    status: int    # pb.LocationStatus enum value
    accuracy_m: Optional[float]  # 0 for sidecar "exact"; None when unset


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_ts_ms(value) -> Optional[int]:
    """Sidecar start_time may be ISO 8601, epoch seconds, or epoch ms.
    Returns ms since the epoch, or None when the value is unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # < 1e11 ≈ before year 5138, so anything below that is seconds.
        return int(value * 1000) if value < 1e11 else int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return None


class LocationResolver:
    """Per-clip GPS resolver. Loads the BigQuery sensor registry once
    at startup and caches it; every clip's location is looked up
    against the sidecar first and falls back to the registry."""

    def __init__(self, snap_to_registry: bool = False) -> None:
        self._registry: dict[str, tuple[float, float]] = {}
        self._snap = snap_to_registry
        self._load_registry()

    def _load_registry(self) -> None:
        """One BigQuery query against the prod argos sensor registry.
        Requires `bigquery.user` (or `bigquery.dataViewer` on the
        dataset) granted to the bridge SA in argos-487318."""
        try:
            client = bigquery.Client()
            sql = (
                f"SELECT {REGISTRY_SR_COL} AS sensor, "
                f"ST_Y(location) AS lat, ST_X(location) AS lon "
                f"FROM `{REGISTRY_TABLE}`"
            )
            log.info("loading registry from BigQuery: %s", REGISTRY_TABLE)
            for row in client.query(sql).result():
                sensor = row.sensor
                if sensor and row.lat is not None and row.lon is not None:
                    self._registry[sensor] = (float(row.lat), float(row.lon))
            log.info("registry loaded: %d stations", len(self._registry))
        except Exception as e:  # noqa: BLE001
            log.error("BigQuery registry load failed: %s", e)
            if REGISTRY_REQUIRED:
                raise
            log.warning(
                "continuing without registry; sidecar-less clips will be skipped"
            )

    def registry_lookup(self, station_id: str) -> Optional[tuple[float, float]]:
        return self._registry.get(short_id(station_id))

    def resolve(
        self, station_id: str, sidecar: Optional[dict]
    ) -> Optional[ResolvedLocation]:
        """Resolve a single clip's location. Returns None when neither
        source has data for this station."""
        sc_loc = (sidecar or {}).get("location") if sidecar else None
        sc_lat = sc_loc.get("latitude") if isinstance(sc_loc, dict) else None
        sc_lon = sc_loc.get("longitude") if isinstance(sc_loc, dict) else None
        sidecar_present = (
            isinstance(sc_lat, (int, float))
            and isinstance(sc_lon, (int, float))
            and not (sc_lat == 0 and sc_lon == 0)
        )

        if sidecar_present:
            assert sc_loc is not None  # for type checker
            lat = float(sc_lat)  # type: ignore[arg-type]
            lon = float(sc_lon)  # type: ignore[arg-type]
            alt = float(sc_loc.get("altitude") or 0.0)
            ts_ms = _parse_ts_ms(sidecar.get("start_time")) if sidecar else None
            if ts_ms is None:
                ts_ms = int(time.time() * 1000)
            accuracy = (
                0.0 if str(sc_loc.get("resolution", "")).lower() == "exact" else None
            )

            # Sanity guard: distance check against registry.
            reg = self._registry.get(short_id(station_id))
            if reg is not None:
                dist = haversine_km(lat, lon, reg[0], reg[1])
                if dist > SANITY_DIST_KM:
                    log.warning(
                        "station=%s sidecar GPS %.4f,%.4f is %.0f km from registry "
                        "(%.4f,%.4f) — flagged but %s",
                        station_id, lat, lon, dist, reg[0], reg[1],
                        "snapping to registry" if self._snap else "using sidecar value",
                    )
                    if self._snap:
                        return ResolvedLocation(
                            latitude=reg[0],
                            longitude=reg[1],
                            altitude_m=0.0,
                            timestamp_ms=ts_ms,
                            provider="registry",
                            status=pb.LOCATION_STATUS_MANUAL,
                            accuracy_m=None,
                        )

            return ResolvedLocation(
                latitude=lat,
                longitude=lon,
                altitude_m=alt,
                timestamp_ms=ts_ms,
                provider="sidecar-gps",
                status=pb.LOCATION_STATUS_CURRENT,
                accuracy_m=accuracy,
            )

        # Fallback to registry.
        reg = self._registry.get(short_id(station_id))
        if reg is None:
            return None
        return ResolvedLocation(
            latitude=reg[0],
            longitude=reg[1],
            altitude_m=0.0,
            timestamp_ms=int(time.time() * 1000),
            provider="registry",
            status=pb.LOCATION_STATUS_MANUAL,
            accuracy_m=None,
        )


def _loc_changed(a: ResolvedLocation, b: ResolvedLocation) -> bool:
    """True when two resolved locations differ enough to be worth a
    LocationUpdate. We compare with a tiny rounding tolerance (~1 cm
    at the equator) so that floating-point noise in the sidecar doesn't
    spam updates."""
    return (
        round(a.latitude, 7) != round(b.latitude, 7)
        or round(a.longitude, 7) != round(b.longitude, 7)
        or round(a.altitude_m, 3) != round(b.altitude_m, 3)
        or a.provider != b.provider
    )


# ---------------------------------------------------------------------------
# GCS source adapter
# ---------------------------------------------------------------------------

@dataclass
class ClipRef:
    """A single clip blob in the source bucket."""
    blob_name: str
    station_id: str
    sidecar_blob_name: str
    source_ts_iso: str = ""  # decoded from blob path if possible


_FNAME_TS = re.compile(r"\.Scell\.(\d{8}_\d{6})")


def _decode_ts(blob_name: str) -> str:
    m = _FNAME_TS.search(blob_name)
    return m.group(1) if m else ""


class GcsClipSource:
    """Lists + downloads .wav blobs (and their .json sidecars) under
    gs://{bucket}/{prefix}/{station_id}/ in lexical order, which for
    these clip names matches chronological order (YYYY/MM/DD/HH/...).

    list_latest() walks the prefix once and takes the most recent
    ``window_hours`` of clips, by file name."""

    def __init__(self, bucket_name: str, prefix: str) -> None:
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._prefix = prefix.rstrip("/")
        log.info("gcs source ready bucket=%s prefix=%s", bucket_name, self._prefix)

    def list_latest(self, station_id: str, window_hours: int) -> list[ClipRef]:
        # GCS keys clips by the original Argos identifier (e.g. SH011),
        # not by our SIM-SHAW-* prefixed simulator id. Translate before
        # listing.
        prefix = f"{self._prefix}/{short_id(station_id)}/"
        clips: list[ClipRef] = []
        for blob in self._client.list_blobs(self._bucket, prefix=prefix):
            if not blob.name.lower().endswith(".wav"):
                continue
            sidecar = blob.name[:-4] + ".json"
            clips.append(
                ClipRef(
                    blob_name=blob.name,
                    station_id=station_id,
                    sidecar_blob_name=sidecar,
                    source_ts_iso=_decode_ts(blob.name),
                )
            )
        clips.sort(key=lambda c: c.blob_name)
        target = max(1, window_hours * 240)
        return clips[-target:]

    def download(self, blob_name: str) -> bytes:
        return self._bucket.blob(blob_name).download_as_bytes(timeout=30)

    def download_sidecar(self, sidecar_blob_name: str) -> Optional[dict]:
        try:
            data = self._bucket.blob(sidecar_blob_name).download_as_bytes(timeout=15)
            return json.loads(data.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.debug("sidecar fetch failed %s: %s", sidecar_blob_name, e)
            return None


# ---------------------------------------------------------------------------
# Audio adapter (WAV -> 16 kHz PCM16, optionally FLAC-encoded)
# ---------------------------------------------------------------------------

def to_pipeline_payload(wav_bytes: bytes, codec: str) -> tuple[bytes, int]:
    """Decode the source 8 kHz WAV, resample to 16 kHz mono PCM16, and
    encode per the requested codec ("pcm16" or "flac"). Returns
    ``(payload_bytes, sample_rate_hz)``."""
    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.int16)
    if sr != SOURCE_SR:
        log.warning("unexpected source sr=%d (expected %d)", sr, SOURCE_SR)
    audio_16k = ss.resample_poly(audio.astype(np.float32), TARGET_SR, sr)
    audio_16k = np.clip(audio_16k, -32768, 32767).astype(np.int16)

    if codec in ("", "pcm16"):
        return audio_16k.tobytes(), TARGET_SR
    if codec == "flac":
        buf = io.BytesIO()
        sf.write(buf, audio_16k, TARGET_SR, format="FLAC", subtype="PCM_16")
        return buf.getvalue(), TARGET_SR
    raise ValueError(f"unsupported codec: {codec}")


# ---------------------------------------------------------------------------
# Proto builders
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _device_location_pb(loc: ResolvedLocation) -> pb.DeviceLocation:
    msg = pb.DeviceLocation(
        latitude=loc.latitude,
        longitude=loc.longitude,
        altitude_meters=loc.altitude_m,
        location_timestamp_ms=loc.timestamp_ms,
        provider=loc.provider,
        status=loc.status,
    )
    if loc.accuracy_m is not None:
        msg.horizontal_accuracy_meters = loc.accuracy_m
    return msg


def _handshake_msg(
    station_id: str, loc: ResolvedLocation, sample_rate_hz: int
) -> pb.ClientStreamMessage:
    # Admin Site column gets the descriptive sentence from stations.py
    # (e.g. "SIMULATED – Shaw AFB / Sumter SC cluster ... SH011 (33.969°N)")
    # with the (cadence/codec) suffix that the Type column splits off.
    # Falls back to the device id when the station isn't in the roster.
    station = BY_ID.get(station_id)
    name = station.description if station and station.description else station_id
    site_tag = f"{name} ({int(CLIP_SECONDS)}s/{CODEC})"
    return pb.ClientStreamMessage(
        handshake=pb.ConnectHandshake(
            device_id=station_id,
            connect_timestamp_ms=_now_ms(),
            app_version="argos-sim/1.0",
            device_model="argos-bridge",
            os_version="argos-sim/1.0",
            assigned_site_label=site_tag,
            location=_device_location_pb(loc),
            auth_token_id=f"uat-test-{station_id}",
            sample_rate_hz=sample_rate_hz,
            frame_duration_ms=int(CLIP_SECONDS * 1000),
            health=pb.DeviceHealth(
                battery_percent=85,
                charging=True,
                network_type=pb.NETWORK_TYPE_ETHERNET,
                thermal_state=pb.THERMAL_STATE_NOMINAL,
                app_version="argos-sim/1.0",
                microphone_active=True,
            ),
        )
    )


def _location_update_msg(
    station_id: str, loc: ResolvedLocation
) -> pb.ClientStreamMessage:
    return pb.ClientStreamMessage(
        location_update=pb.LocationUpdate(
            device_id=station_id,
            update_timestamp_ms=_now_ms(),
            location=_device_location_pb(loc),
        )
    )


def _audio_frame_msg(
    station_id: str,
    sequence: int,
    payload: bytes,
    sample_rate_hz: int,
    capture_ts_ms: int,
) -> pb.ClientStreamMessage:
    return pb.ClientStreamMessage(
        audio_frame=pb.AudioFrame(
            device_id=station_id,
            capture_timestamp_ms=capture_ts_ms,
            sequence_number=sequence,
            sample_rate_hz=sample_rate_hz,
            pcm16_mono=payload,
            codec=CODEC,
        )
    )


# ---------------------------------------------------------------------------
# Per-station long-lived bidi session
# ---------------------------------------------------------------------------

@dataclass
class StationStats:
    station_id: str
    clips_sent: int = 0
    clips_failed: int = 0
    location_updates: int = 0
    sidecar_loc_used: int = 0
    registry_loc_used: int = 0
    no_loc_skipped: int = 0
    bytes_sent: int = 0
    sessions: int = 0
    last_clip_blob: str = ""
    last_sent_at: float = 0.0


async def _open_channel() -> grpc.aio.Channel:
    if GATEWAY_TLS:
        return grpc.aio.secure_channel(GATEWAY_URL, grpc.ssl_channel_credentials())
    return grpc.aio.insecure_channel(GATEWAY_URL)


async def _run_session(
    station_id: str,
    source: GcsClipSource,
    resolver: LocationResolver,
    shutdown: asyncio.Event,
    stats: StationStats,
) -> None:
    """One long-lived bidi stream per station. Lists clips, fetches
    each clip's sidecar, resolves location, opens a stream, sends a
    handshake with the first clip's location, then emits LocationUpdate
    + AudioFrame for each clip, pacing at ~CLIP_SECONDS wall clock."""
    queue = await asyncio.to_thread(source.list_latest, station_id, WINDOW_HOURS)
    if not queue:
        log.info("station=%s no clips found; sleeping then retry", station_id)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=CLIP_SECONDS * 10)
            return
        except asyncio.TimeoutError:
            return

    # Pre-resolve the first clip's location so the handshake can carry it.
    first_sidecar = await asyncio.to_thread(
        source.download_sidecar, queue[0].sidecar_blob_name
    )
    first_loc = resolver.resolve(station_id, first_sidecar)
    if first_loc is None:
        log.error(
            "station=%s neither sidecar nor registry has a location; "
            "cannot open session", station_id
        )
        # Drop a session-worth of clips so we don't tight-loop.
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=CLIP_SECONDS * 60)
        except asyncio.TimeoutError:
            pass
        return

    stats.sessions += 1
    log.info(
        "station=%s session %d open queue=%d first_loc=%.4f,%.4f provider=%s",
        station_id, stats.sessions, len(queue),
        first_loc.latitude, first_loc.longitude, first_loc.provider,
    )

    msg_queue: asyncio.Queue = asyncio.Queue(maxsize=8)

    async def request_iter() -> AsyncIterator[pb.ClientStreamMessage]:
        while True:
            msg = await msg_queue.get()
            if msg is None:
                return
            yield msg

    async def producer() -> None:
        last_loc = first_loc
        sequence = 0
        try:
            await msg_queue.put(_handshake_msg(station_id, first_loc, TARGET_SR))
            for idx, clip in enumerate(queue):
                if shutdown.is_set():
                    break
                cycle_start = time.monotonic()
                sidecar = (
                    first_sidecar if idx == 0
                    else await asyncio.to_thread(
                        source.download_sidecar, clip.sidecar_blob_name
                    )
                )
                loc = resolver.resolve(station_id, sidecar)
                if loc is None:
                    stats.no_loc_skipped += 1
                    log.debug("station=%s no location for clip %s", station_id, clip.blob_name)
                    # Skip this clip's audio but still pace.
                else:
                    if loc.provider == "sidecar-gps":
                        stats.sidecar_loc_used += 1
                    else:
                        stats.registry_loc_used += 1
                    if idx > 0 and _loc_changed(loc, last_loc):
                        await msg_queue.put(_location_update_msg(station_id, loc))
                        stats.location_updates += 1
                        last_loc = loc

                    try:
                        wav_bytes = await asyncio.to_thread(
                            source.download, clip.blob_name
                        )
                        payload, sr = await asyncio.to_thread(
                            to_pipeline_payload, wav_bytes, CODEC
                        )
                        sequence += 1
                        await msg_queue.put(
                            _audio_frame_msg(
                                station_id, sequence, payload, sr,
                                capture_ts_ms=loc.timestamp_ms,
                            )
                        )
                        stats.clips_sent += 1
                        stats.bytes_sent += len(payload)
                        stats.last_clip_blob = clip.blob_name
                        stats.last_sent_at = time.time()
                    except Exception as e:  # noqa: BLE001
                        stats.clips_failed += 1
                        log.warning(
                            "clip_failed station=%s blob=%s err=%s",
                            station_id, clip.blob_name, e,
                        )

                # Pace.
                elapsed = time.monotonic() - cycle_start
                remaining = CLIP_SECONDS - elapsed
                if remaining > 0:
                    try:
                        await asyncio.wait_for(shutdown.wait(), timeout=remaining)
                        break
                    except asyncio.TimeoutError:
                        pass
        finally:
            await msg_queue.put(None)

    channel = await _open_channel()
    try:
        stub = pb_grpc.DroneAudioStreamStub(channel)
        prod_task = asyncio.create_task(producer())
        call = stub.StreamAudio(request_iter())
        try:
            async for _resp in call:
                pass
        finally:
            # The server may close before the producer has finished
            # walking the clip queue (cold-start eviction, transient
            # network error, etc.). Cancel the producer so it doesn't
            # leak through to the next session attempt.
            if not prod_task.done():
                prod_task.cancel()
                try:
                    await prod_task
                except asyncio.CancelledError:
                    pass
    finally:
        await channel.close()


async def replay_station(
    station_id: str,
    source: GcsClipSource,
    resolver: LocationResolver,
    shutdown: asyncio.Event,
    stats: StationStats,
) -> None:
    # Stagger the first session start so 33 stations don't all open
    # streams in lockstep on the same second.
    try:
        await asyncio.wait_for(
            shutdown.wait(), timeout=random.uniform(0.0, CLIP_SECONDS)
        )
        return
    except asyncio.TimeoutError:
        pass

    backoff_idx = 0
    while not shutdown.is_set():
        t0 = time.monotonic()
        try:
            await _run_session(station_id, source, resolver, shutdown, stats)
            session_secs = time.monotonic() - t0
            # A session that ran for a while is healthy; reset backoff.
            if session_secs > 30:
                backoff_idx = 0
        except Exception as e:  # noqa: BLE001
            log.warning("station=%s session ended err=%s", station_id, e)
        if shutdown.is_set():
            return
        backoff = RECONNECT_BACKOFF_S[
            min(backoff_idx, len(RECONNECT_BACKOFF_S) - 1)
        ]
        backoff_idx += 1
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=backoff)
            return
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

async def report_loop(stats: list[StationStats], shutdown: asyncio.Event) -> None:
    period_s = 60
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=period_s)
            return
        except asyncio.TimeoutError:
            pass
        total_sent = sum(s.clips_sent for s in stats)
        total_failed = sum(s.clips_failed for s in stats)
        total_bytes = sum(s.bytes_sent for s in stats)
        sidecar = sum(s.sidecar_loc_used for s in stats)
        registry = sum(s.registry_loc_used for s in stats)
        no_loc = sum(s.no_loc_skipped for s in stats)
        log_updates = sum(s.location_updates for s in stats)
        log.info(
            "report: %d stations  clips=%d fail=%d bytes=%.1f MB  "
            "loc[sidecar=%d registry=%d skipped=%d updates=%d]",
            len(stats), total_sent, total_failed, total_bytes / 1e6,
            sidecar, registry, no_loc, log_updates,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if STATIONS_ENV:
        wanted = {s.strip() for s in STATIONS_ENV.split(",") if s.strip()}
        station_ids = [s.station_id for s in STATIONS if s.station_id in wanted]
        if not station_ids:
            log.error("BRIDGE_STATIONS=%r matched none of the roster", STATIONS_ENV)
            return 1
    else:
        station_ids = [s.station_id for s in STATIONS]

    log.info(
        "argos-bridge starting  gateway=%s tls=%s codec=%s stations=%d "
        "window=%dh snap_to_registry=%s",
        GATEWAY_URL, GATEWAY_TLS, CODEC,
        len(station_ids), WINDOW_HOURS, SNAP_TO_REGISTRY,
    )

    source = GcsClipSource(GCS_BUCKET, GCS_PREFIX)
    resolver = LocationResolver(snap_to_registry=SNAP_TO_REGISTRY)
    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    stats = [StationStats(station_id=sid) for sid in station_ids]
    tasks = [
        asyncio.create_task(replay_station(sid, source, resolver, shutdown, s))
        for sid, s in zip(station_ids, stats)
    ]
    tasks.append(asyncio.create_task(report_loop(stats, shutdown)))

    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("argos-bridge exiting")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
