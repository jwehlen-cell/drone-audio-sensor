#!/usr/bin/env python3
"""Live-pull subscriber for the prod Argos data stream.

Subscribes to the prod Argos Pub/Sub notification topic, authenticating
as the ``argos-bridge@argos-487318`` service account whose JSON key was
handed to us by the Argos team. For every clip notification:

  1. Fetch the .wav and the matching .json sidecar from
     ``gs://aftac-argos-dataflow-unzipped`` (same SA).
  2. Resample 8 kHz → 16 kHz so YAMNet accepts it.
  3. Forward to our argosuat gateway under the
     ``ARGOS-SHAW-SH###`` device id for that station, with a
     synthesized DeviceHealth so the dashboard renders full
     battery/signal/temperature cells the same way real phones do.

Auth strategy:
  - The SA key lives as a Secret Manager secret in argosuat
    (``argos-live-pull-sa-key`` by default). We fetch the JSON at
    startup, write it to a tempfile, and point
    ``GOOGLE_APPLICATION_CREDENTIALS`` at it. Pub/Sub + Storage clients
    then authenticate as ``argos-bridge@argos-487318``.
  - Alternatively, ``--sa-key-path`` reads the JSON from a local file,
    handy during initial bench testing.

Env vars (all optional, defaults shown):
  ARGOS_LIVE_TOPIC               projects/argos-487318/topics/aftac-argos-unzipped
  ARGOS_LIVE_SUBSCRIPTION        projects/argos-487318/subscriptions/aftac-argos-unzipped-drone-sensor
  ARGOS_LIVE_BUCKET              aftac-argos-dataflow-unzipped
  ARGOS_LIVE_SA_KEY_SECRET       projects/argosuat/secrets/argos-live-pull-sa-key/versions/latest
  BRIDGE_GATEWAY_URL             drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app
  BRIDGE_GATEWAY_TLS             true
  BRIDGE_CODEC                   pcm16
  ARGOS_LIVE_MAX_INFLIGHT        8     (concurrent forwards)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import random
import signal
import sys
import tempfile
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

import grpc
import grpc.aio
import numpy as np
import soundfile as sf
import scipy.signal as ss

# Reach the generated proto. The startup script regenerates these into
# scripts/ at deploy time; if you're running locally and the protos
# aren't there yet, run scripts/argos_bridge/startup.sh's protoc step
# first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import drone_audio_pb2 as pb  # type: ignore
import drone_audio_pb2_grpc as pb_grpc  # type: ignore

log = logging.getLogger("argos-live")

TOPIC = os.environ.get(
    "ARGOS_LIVE_TOPIC", "projects/argos-487318/topics/aftac-argos-unzipped"
)
SUBSCRIPTION = os.environ.get(
    "ARGOS_LIVE_SUBSCRIPTION",
    "projects/argos-487318/subscriptions/aftac-argos-unzipped-drone-sensor",
)
BUCKET = os.environ.get("ARGOS_LIVE_BUCKET", "aftac-argos-dataflow-unzipped")
SA_KEY_SECRET = os.environ.get(
    "ARGOS_LIVE_SA_KEY_SECRET",
    "projects/argosuat/secrets/argos-live-pull-sa-key/versions/latest",
)
GATEWAY_URL = os.environ.get(
    "BRIDGE_GATEWAY_URL",
    "drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app",
)
GATEWAY_TLS = os.environ.get("BRIDGE_GATEWAY_TLS", "true").lower() in {"true", "1", "yes"}
CODEC = os.environ.get("BRIDGE_CODEC", "pcm16")
MAX_INFLIGHT = int(os.environ.get("ARGOS_LIVE_MAX_INFLIGHT", "8"))

SOURCE_SR = 8000
TARGET_SR = 16000
CLIP_SECONDS = 4.096
SITE_LABEL = "Shaw"


# ---------------------------------------------------------------------------
# Service-account key loading (Secret Manager → tempfile → ADC)
# ---------------------------------------------------------------------------

def load_sa_key(secret_name: Optional[str], local_path: Optional[str]) -> str:
    """Pull the JSON SA key onto disk and return its path. ADC env var
    (GOOGLE_APPLICATION_CREDENTIALS) is set so subsequent Google client
    libraries authenticate as the Argos-side SA."""
    if local_path:
        path = os.path.abspath(local_path)
        if not Path(path).is_file():
            raise SystemExit(f"--sa-key-path not found: {path}")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        log.info("using SA key from local file: %s", path)
        return path

    if not secret_name:
        raise SystemExit(
            "no SA key source: pass --sa-key-path or set ARGOS_LIVE_SA_KEY_SECRET"
        )

    # Use whatever ADC the host VM has (its own SA, gcloud auth, etc.)
    # to pull the secret. Argosuat VMs run as the drone-sensor-dev-
    # argos-bridge SA which has secretmanager.secretAccessor.
    from google.cloud import secretmanager  # type: ignore
    sm = secretmanager.SecretManagerServiceClient()
    log.info("fetching SA key from secret: %s", secret_name)
    resp = sm.access_secret_version(request={"name": secret_name})
    payload = resp.payload.data.decode("utf-8")
    # Sanity-check it actually parses as a key file.
    try:
        parsed = json.loads(payload)
        client_email = parsed.get("client_email", "<unknown>")
    except json.JSONDecodeError as e:
        raise SystemExit(f"SA key payload isn't valid JSON: {e}")

    # Tempfile is mode 0600 by default on POSIX.
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    f.write(payload)
    f.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = f.name
    log.info("SA key written to %s (client_email=%s)", f.name, client_email)
    return f.name


# ---------------------------------------------------------------------------
# Sensor → device-id mapping
# ---------------------------------------------------------------------------

def device_id_for(sensor: str) -> str:
    """Argos clip sidecars carry the raw sensor id (e.g. ``SH011``). We
    stream it under our ARGOS-SHAW-SH### convention so the upstream
    provenance is obvious on the admin dashboard (and so the live
    Argos sensors don't get visually mixed up with simulated phones,
    which use the ``SIM-`` prefix)."""
    sensor = sensor.strip()
    if not sensor.startswith("SH"):
        # Should never happen for argos clips; defensive.
        sensor = f"SH{sensor}"
    return f"ARGOS-SHAW-{sensor}"


# ---------------------------------------------------------------------------
# Sensor → (lat, lon) fallback
# ---------------------------------------------------------------------------
#
# Argos clip sidecars don't carry sensor coordinates, so a plain
# ``sidecar.get("location")`` returns None and the gateway-side
# device doc gets ``current_location=(0,0)`` — Null Island. That
# wipes the dashboard map pin and breaks the Shaw site chip's
# spatial layout. Fall back to scripts/argos_bridge/stations.py,
# the authoritative roster of public Argos station positions,
# keyed on the ARGOS-SHAW-* prefixed id so we can look up directly
# by device_id.

def _build_sensor_location_map() -> dict[str, dict]:
    """Map {device_id: {latitude, longitude}} built once at import
    time from the bridge roster."""
    import sys as _sys
    from pathlib import Path as _Path
    _bridge_dir = _Path(__file__).resolve().parent.parent / "argos_bridge"
    if str(_bridge_dir) not in _sys.path:
        _sys.path.insert(0, str(_bridge_dir))
    try:
        from stations import STATIONS  # type: ignore
    except ImportError:
        return {}
    return {
        st.station_id: {"latitude": st.latitude, "longitude": st.longitude}
        for st in STATIONS
    }


_SENSOR_LOC_FALLBACK = _build_sensor_location_map()


def sensor_location(device_id: str, sidecar: Optional[dict]) -> Optional[dict]:
    """Sidecar wins when it has a real position; otherwise fall back
    to the stations.py roster. Treats (0,0) as 'no position'
    (Argos schema uses 0/0 as missing, not the literal point in the
    Atlantic, so don't let it through)."""
    if isinstance(sidecar, dict):
        cand = sidecar.get("location")
        if isinstance(cand, dict):
            lat = float(cand.get("latitude", 0.0))
            lon = float(cand.get("longitude", 0.0))
            if (lat, lon) != (0.0, 0.0):
                return cand
    return _SENSOR_LOC_FALLBACK.get(device_id)


# ---------------------------------------------------------------------------
# SOH synthesis — argos sensors don't report battery/temp/RSSI, so we
# render plausible values for the dashboard
# ---------------------------------------------------------------------------

@dataclass
class StationSohState:
    """Per-station SOH state that drifts realistically over time."""
    battery_percent: int
    battery_temp_deci_c: int        # tenths of a degree C
    battery_voltage_mv: int
    cellular_rssi_dbm: int
    last_update: float


class SohSynth:
    """Pseudo-realistic SOH generator for argos sensors. Each station
    gets a sticky baseline (battery, temp band, RSSI) that drifts
    slowly so dashboard cells don't flicker between samples."""

    def __init__(self) -> None:
        self._state: dict[str, StationSohState] = {}
        self._rng = random.Random(0xA46055)

    def _init_state(self, device_id: str) -> StationSohState:
        # Base baseline: phones on solar packs hover 70–95% nominally.
        return StationSohState(
            battery_percent=self._rng.randint(70, 95),
            battery_temp_deci_c=self._rng.randint(220, 380),   # 22.0–38.0 °C
            battery_voltage_mv=self._rng.randint(3700, 4150),
            cellular_rssi_dbm=self._rng.randint(-105, -70),
            last_update=time.monotonic(),
        )

    def snapshot(self, device_id: str) -> StationSohState:
        st = self._state.get(device_id) or self._init_state(device_id)
        now = time.monotonic()
        dt = now - st.last_update
        # Battery drifts 1% per ~5 min, temp wanders ±2 °C/hour, RSSI
        # ±2 dB/min. Clamped so we don't drift off the dashboard.
        st.battery_percent = max(
            10, min(100, st.battery_percent + (self._rng.random() - 0.5) * (dt / 300))
        )
        st.battery_temp_deci_c = max(
            150, min(500, st.battery_temp_deci_c + int((self._rng.random() - 0.5) * (dt / 30)))
        )
        st.battery_voltage_mv = max(
            3300, min(4250, st.battery_voltage_mv + int((self._rng.random() - 0.5) * 5))
        )
        st.cellular_rssi_dbm = max(
            -120, min(-50, st.cellular_rssi_dbm + int((self._rng.random() - 0.5) * (dt / 30)))
        )
        st.last_update = now
        self._state[device_id] = st
        return st


# ---------------------------------------------------------------------------
# Audio adapter (same shape as argos_bridge/bridge.py:to_pipeline_payload)
# ---------------------------------------------------------------------------

def to_pipeline_payload(wav_bytes: bytes, codec: str) -> tuple[bytes, int]:
    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.int16)
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
# Per-clip forwarder
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


async def _channel() -> grpc.aio.Channel:
    if GATEWAY_TLS:
        return grpc.aio.secure_channel(GATEWAY_URL, grpc.ssl_channel_credentials())
    return grpc.aio.insecure_channel(GATEWAY_URL)


def _build_handshake(
    device_id: str,
    sensor_loc: Optional[dict],
    sample_rate_hz: int,
    soh: StationSohState,
) -> pb.ClientStreamMessage:
    loc = pb.DeviceLocation(
        latitude=float(sensor_loc.get("latitude", 0.0)) if sensor_loc else 0.0,
        longitude=float(sensor_loc.get("longitude", 0.0)) if sensor_loc else 0.0,
        altitude_meters=float(sensor_loc.get("altitude", 0.0)) if sensor_loc else 0.0,
        location_timestamp_ms=_now_ms(),
        provider="argos-live",
        status=pb.LOCATION_STATUS_CURRENT,
    )
    health = pb.DeviceHealth(
        battery_percent=int(soh.battery_percent),
        charging=True,
        network_type=pb.NETWORK_TYPE_CELLULAR_LTE,
        thermal_state=pb.THERMAL_STATE_NOMINAL,
        app_version="argos-live/1.0",
        microphone_active=True,
        battery_temperature_deci_c=int(soh.battery_temp_deci_c),
        battery_voltage_mv=int(soh.battery_voltage_mv),
        battery_health=2,  # GOOD
        cellular_rssi_dbm=int(soh.cellular_rssi_dbm),
        wifi_rssi_dbm=0,
        free_disk_bytes=0,
        tx_bytes_cumulative=0,
    )
    return pb.ClientStreamMessage(
        handshake=pb.ConnectHandshake(
            device_id=device_id,
            connect_timestamp_ms=_now_ms(),
            app_version="argos-live/1.0",
            device_model="argos-live",
            os_version="argos-live/1.0",
            assigned_site_label=(
                f"ARGOS live pull – Shaw cluster, {device_id} "
                f"({int(CLIP_SECONDS)}s/{CODEC})"
            ),
            location=loc,
            auth_token_id=f"argos-live-{device_id}",
            sample_rate_hz=sample_rate_hz,
            frame_duration_ms=int(CLIP_SECONDS * 1000),
            health=health,
        )
    )


def _build_audio_frame(
    device_id: str,
    payload: bytes,
    sample_rate_hz: int,
    capture_ts_ms: int,
    sequence: int,
) -> pb.ClientStreamMessage:
    return pb.ClientStreamMessage(
        audio_frame=pb.AudioFrame(
            device_id=device_id,
            capture_timestamp_ms=capture_ts_ms,
            sequence_number=sequence,
            sample_rate_hz=sample_rate_hz,
            pcm16_mono=payload,
            codec=CODEC,
        )
    )


async def forward_one_clip(
    device_id: str,
    wav_bytes: bytes,
    sidecar: Optional[dict],
    soh: StationSohState,
    sequence: int,
) -> None:
    """Open a per-clip stream, send handshake + 1 audio frame, close.
    Mirrors the replay-fleet pattern; the gateway's per-stream state
    stays simple and the asyncio task per clip means we don't block
    the Pub/Sub callback while waiting on a frame ack."""
    payload, sr = to_pipeline_payload(wav_bytes, CODEC)
    capture_ts_ms = _now_ms()
    if sidecar and isinstance(sidecar.get("start_time"), (int, float)):
        # Sidecar start_time is epoch seconds in the argos schema.
        capture_ts_ms = int(sidecar["start_time"] * 1000)

    loc = sensor_location(device_id, sidecar)
    handshake = _build_handshake(device_id, loc, sr, soh)
    frame = _build_audio_frame(device_id, payload, sr, capture_ts_ms, sequence)

    channel = await _channel()
    try:
        stub = pb_grpc.DroneAudioStreamStub(channel)
        async def _req():
            yield handshake
            yield frame
        async for _ in stub.StreamAudio(_req()):
            pass  # drain ServerCommand replies
    finally:
        await channel.close()


# ---------------------------------------------------------------------------
# Pub/Sub subscriber
# ---------------------------------------------------------------------------

class LiveSubscriber:
    def __init__(self, max_inflight: int = MAX_INFLIGHT) -> None:
        from google.cloud import pubsub_v1, storage  # type: ignore
        self._subscriber = pubsub_v1.SubscriberClient()
        self._storage = storage.Client()
        self._bucket = self._storage.bucket(BUCKET)
        self._soh = SohSynth()
        # asyncio.Semaphore binds to the running event loop at
        # construction time on Python 3.9. Creating it here (on the
        # main thread, before run() spins up a new loop) would bind it
        # to the default loop, and the first `async with self._sem`
        # inside _handle would then fail "Future attached to a
        # different loop". Defer creation to run() where the new loop
        # is the running one.
        self._max_inflight = max_inflight
        self._sem: Optional[asyncio.Semaphore] = None
        # Per-station sequence counters so each device_id's frames count up.
        self._seq: dict[str, int] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stats = {"received": 0, "forwarded": 0, "failed": 0}

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._sem = asyncio.Semaphore(self._max_inflight)
        # Pub/Sub's StreamingPull is sync (threadpool callbacks); we
        # bridge to asyncio so the audio forwarders can use grpc.aio.
        streaming_pull = self._subscriber.subscribe(
            SUBSCRIPTION, callback=self._enqueue
        )
        log.info("subscribed: %s (inflight=%d)", SUBSCRIPTION, MAX_INFLIGHT)
        log.info("forwarding to gateway=%s tls=%s codec=%s", GATEWAY_URL, GATEWAY_TLS, CODEC)

        try:
            self._loop.run_until_complete(self._main(streaming_pull))
        finally:
            streaming_pull.cancel()
            with contextlib.suppress(Exception):
                streaming_pull.result(timeout=5)
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _main(self, streaming_pull) -> None:
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._loop.add_signal_handler(sig, stop.set)

        # Periodic report
        async def report() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                    return
                except asyncio.TimeoutError:
                    pass
                log.info(
                    "stats received=%d forwarded=%d failed=%d",
                    self._stats["received"], self._stats["forwarded"], self._stats["failed"],
                )

        reporter = asyncio.create_task(report())
        await stop.wait()
        reporter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter

    def _enqueue(self, message) -> None:
        """Pub/Sub callback (runs on subscriber threadpool). Hand off to
        the asyncio loop and ack immediately — argos retries on the
        producer side if we fail, and re-delivery would just give us
        the same clip we're about to drop anyway."""
        self._stats["received"] += 1
        if self._loop is None:
            message.nack()
            return
        future = asyncio.run_coroutine_threadsafe(
            self._handle(message), self._loop
        )
        # Don't block the subscriber threadpool waiting on the audio
        # forward to finish; the semaphore inside _handle bounds the
        # in-flight pool. Ack now.
        message.ack()
        future.add_done_callback(self._after_handle)

    def _after_handle(self, future: Future) -> None:
        exc = future.exception()
        if exc is not None:
            self._stats["failed"] += 1
            log.warning("handle failed: %s", exc)
        else:
            self._stats["forwarded"] += 1

    async def _handle(self, message) -> None:
        async with self._sem:
            object_id = message.attributes.get("objectId") or message.attributes.get("object_id")
            if not object_id or not object_id.lower().endswith(".wav"):
                return
            sensor = self._sensor_from_path(object_id)
            if not sensor:
                log.debug("no sensor in path %s", object_id)
                return
            device_id = device_id_for(sensor)

            wav = await asyncio.to_thread(self._download, object_id)
            sidecar_path = object_id[:-4] + ".json"
            sidecar_bytes = await asyncio.to_thread(
                self._download_optional, sidecar_path
            )
            sidecar = json.loads(sidecar_bytes) if sidecar_bytes else None
            soh = self._soh.snapshot(device_id)
            self._seq[device_id] = self._seq.get(device_id, 0) + 1
            try:
                await forward_one_clip(
                    device_id, wav, sidecar, soh, self._seq[device_id]
                )
            except Exception as e:  # noqa: BLE001
                log.warning("forward failed for %s: %s", device_id, e)
                raise

    def _download(self, blob_name: str) -> bytes:
        return self._bucket.blob(blob_name).download_as_bytes(timeout=30)

    def _download_optional(self, blob_name: str) -> Optional[bytes]:
        try:
            return self._bucket.blob(blob_name).download_as_bytes(timeout=15)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _sensor_from_path(object_id: str) -> Optional[str]:
        # Path shape per spec:
        #   ensco/SH/<STATION>/YYYY/MM/DD/HH/<STATION>.Scell.<...>.wav
        parts = object_id.split("/")
        for i, p in enumerate(parts):
            if p == "SH" and i + 1 < len(parts):
                return parts[i + 1]
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sa-key-path",
        help="Local JSON SA key file. Skips Secret Manager lookup.",
    )
    parser.add_argument(
        "--sa-key-secret",
        default=SA_KEY_SECRET,
        help="Secret Manager resource for the argos-side SA key.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Python logging level.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    load_sa_key(args.sa_key_secret, args.sa_key_path)
    LiveSubscriber().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
