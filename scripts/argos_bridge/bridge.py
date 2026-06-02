#!/usr/bin/env python3
"""Argos UAT pull-based replay bridge.

For each of 33 simulated SH-* stations, list ~N hours of historical
clips from the prod Argos GCS bucket (read-only), resample them from
the source 8 kHz to the 16 kHz the YAMNet inference worker requires,
and stream them through the local DroneAudioStream gateway in real
time. When a station's queue is exhausted, the source is re-listed
and the loop starts over.

Topology (everything inside argosuat unless marked):

    [argos prod GCS]  <-- read-only --  [argos bridge VM]  -- gRPC -->  [gateway]
    (cross-project)                     (this script)                  (argosuat)
                                                                       |
                                                                       v
                                                                   [Redis stream]
                                                                       |
                                                                       v
                                                                   [inference]
                                                                       |
                                                                       v
                                                                   [admin UI]

Auth: ``GATEWAY_REQUIRE_AUTH`` is OFF in argosuat, so the bridge sends
the device_id in the handshake but does not sign a JWT. The PKI
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
  GOOGLE_CLOUD_PROJECT      argosuat
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import grpc
import numpy as np
import soundfile as sf
import scipy.signal as ss
from google.cloud import storage

# Local imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations import STATIONS, BY_ID

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

SOURCE_SR = 8000      # GCS clips are 8 kHz
TARGET_SR = 16000     # YAMNet requires 16 kHz; bridge resamples on the way in
CLIP_SECONDS = 4.096  # nominal clip duration; used for inter-clip pacing
SITE_LABEL = "SH"     # operator spec: every SH-* station's Site column = "SH"

# Connect retries on per-frame streams. The gateway's Cloud Run cold
# start can take a few seconds when scale-from-zero hits.
RPC_TIMEOUT_S = 30
RETRY_BACKOFF_S = (1.0, 2.0, 5.0)


# ---------------------------------------------------------------------------
# GCS source adapter
# ---------------------------------------------------------------------------

@dataclass
class ClipRef:
    """A single clip blob in the source bucket."""
    blob_name: str
    station_id: str
    source_ts_iso: str = ""  # decoded from blob path if possible


_FNAME_TS = re.compile(r"\.Scell\.(\d{8}_\d{6})")


def _decode_ts(blob_name: str) -> str:
    m = _FNAME_TS.search(blob_name)
    return m.group(1) if m else ""


class GcsClipSource:
    """Lists + downloads .wav blobs under
    gs://{bucket}/{prefix}/{station_id}/ in lexical order, which for
    these clip names matches chronological order (YYYY/MM/DD/HH/...).

    list_latest() walks back from the current UTC hour until it
    accumulates at least ``window_hours`` of clips (or hits the 7-day
    backstop). Returned in chronological order.
    """

    def __init__(self, bucket_name: str, prefix: str) -> None:
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._prefix = prefix.rstrip("/")
        log.info("gcs source ready bucket=%s prefix=%s", bucket_name, self._prefix)

    def list_latest(self, station_id: str, window_hours: int) -> list[ClipRef]:
        # We don't know which hours are populated without listing, so
        # do one prefix-bounded list ordered by name (== time) and take
        # the tail. List with a depth-limited prefix to avoid scanning
        # the whole bucket: prefix=ensco/SH/<station_id>/.
        prefix = f"{self._prefix}/{station_id}/"
        clips: list[ClipRef] = []
        for blob in self._client.list_blobs(self._bucket, prefix=prefix):
            if not blob.name.lower().endswith(".wav"):
                continue
            clips.append(
                ClipRef(
                    blob_name=blob.name,
                    station_id=station_id,
                    source_ts_iso=_decode_ts(blob.name),
                )
            )
        clips.sort(key=lambda c: c.blob_name)
        # Take the tail: assume ~4 clips/min, so window_hours * 240
        # gives us a soft cap that still picks up the freshest data.
        target = max(1, window_hours * 240)
        return clips[-target:]

    def download(self, blob_name: str) -> bytes:
        return self._bucket.blob(blob_name).download_as_bytes(timeout=30)


# ---------------------------------------------------------------------------
# Audio adapter (WAV -> 16 kHz PCM16, optionally FLAC-encoded)
# ---------------------------------------------------------------------------

def to_pipeline_payload(wav_bytes: bytes, codec: str) -> tuple[bytes, int]:
    """Decode the source 8 kHz WAV, resample to 16 kHz mono PCM16, and
    encode per the requested codec ("pcm16" or "flac"). Returns
    ``(payload_bytes, sample_rate_hz)``. The pipeline downstream uses
    sample_rate_hz to drive the YAMNet inference worker."""
    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.int16)
    if sr != SOURCE_SR:
        # Shouldn't happen — the source is spec'd as 8 kHz. But if a
        # mislabelled clip slips through, resample from whatever it is.
        log.warning("unexpected source sr=%d (expected %d)", sr, SOURCE_SR)
    # 8 kHz -> 16 kHz is a 2x upsample; resample_poly is fast and clean.
    audio_16k = ss.resample_poly(audio.astype(np.float32), TARGET_SR, sr)
    # Clip to int16 range; resample_poly can produce small overshoot
    # depending on the filter, ~0.5 dB at most.
    audio_16k = np.clip(audio_16k, -32768, 32767).astype(np.int16)

    if codec == "pcm16" or codec == "":
        return audio_16k.tobytes(), TARGET_SR
    if codec == "flac":
        buf = io.BytesIO()
        sf.write(
            buf, audio_16k, TARGET_SR, format="FLAC", subtype="PCM_16"
        )
        return buf.getvalue(), TARGET_SR
    raise ValueError(f"unsupported codec: {codec}")


# ---------------------------------------------------------------------------
# Per-station gRPC streaming
# ---------------------------------------------------------------------------

def _channel(gateway: str, use_tls: bool) -> grpc.Channel:
    if use_tls:
        return grpc.secure_channel(gateway, grpc.ssl_channel_credentials())
    return grpc.insecure_channel(gateway)


def _now_ms() -> int:
    return int(time.time() * 1000)


def stream_one_clip(
    *,
    station_id: str,
    payload: bytes,
    sample_rate_hz: int,
    sequence_number: int,
    codec: str,
) -> tuple[bool, int]:
    """Open one short-lived stream and send handshake + 1 AudioFrame.
    Returns (ok, bytes_sent). Matches the replay_fleet pattern so the
    gateway's per-stream state stays simple under fan-in from 33 stations."""
    station = BY_ID[station_id]
    site_tag = f"{station_id} ({int(CLIP_SECONDS)}s/{codec})"

    def request_iter():
        yield pb.ClientStreamMessage(
            handshake=pb.ConnectHandshake(
                device_id=station_id,
                connect_timestamp_ms=_now_ms(),
                app_version="argos-sim/1.0",
                device_model="argos-bridge",
                os_version="argos-sim/1.0",
                assigned_site_label=site_tag,
                location=pb.DeviceLocation(
                    latitude=station.latitude,
                    longitude=station.longitude,
                    horizontal_accuracy_meters=10.0,
                    location_timestamp_ms=_now_ms(),
                    provider="argos-bridge",
                    status=pb.LOCATION_STATUS_CURRENT,
                ),
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
        yield pb.ClientStreamMessage(
            audio_frame=pb.AudioFrame(
                device_id=station_id,
                capture_timestamp_ms=_now_ms(),
                sequence_number=sequence_number,
                sample_rate_hz=sample_rate_hz,
                pcm16_mono=payload,
                codec=codec,
            )
        )

    ch = _channel(GATEWAY_URL, GATEWAY_TLS)
    try:
        stub = pb_grpc.DroneAudioStreamStub(ch)
        for _resp in stub.StreamAudio(request_iter(), timeout=RPC_TIMEOUT_S):
            # Drain any ServerCommand (e.g. FrameAck). For the per-frame
            # stream we just want the gateway to see it through.
            pass
        return True, len(payload)
    except grpc.RpcError as e:
        log.warning(
            "stream_failed station=%s code=%s detail=%s",
            station_id,
            getattr(e, "code", lambda: "?")(),
            getattr(e, "details", lambda: "")(),
        )
        return False, 0
    finally:
        ch.close()


# ---------------------------------------------------------------------------
# Replay loop per station
# ---------------------------------------------------------------------------

@dataclass
class StationStats:
    station_id: str
    clips_sent: int = 0
    clips_failed: int = 0
    bytes_sent: int = 0
    loops: int = 0
    last_clip_blob: str = ""
    last_sent_at: float = 0.0


async def replay_station(
    station_id: str,
    source: GcsClipSource,
    shutdown: asyncio.Event,
    stats: StationStats,
) -> None:
    """One station's replay loop. List clips → send one at a time at
    ~real-time pace (1 clip per CLIP_SECONDS wall clock). Re-list when
    the queue is exhausted or every REFRESH_INTERVAL_S, whichever comes
    first. Random initial phase staggers the 33 stations so per-clip
    gateway load is smooth rather than spiking on the second."""
    # Stagger first send across the clip window so the 33 stations
    # don't all hit the gateway in lockstep.
    try:
        await asyncio.wait_for(
            shutdown.wait(), timeout=random.uniform(0.0, CLIP_SECONDS)
        )
        return  # shutdown fired during stagger
    except asyncio.TimeoutError:
        pass

    sequence = 0
    last_refresh = 0.0
    queue: list[ClipRef] = []

    while not shutdown.is_set():
        now = time.monotonic()
        if not queue or (now - last_refresh) > REFRESH_INTERVAL_S:
            try:
                queue = await asyncio.to_thread(
                    source.list_latest, station_id, WINDOW_HOURS
                )
            except Exception as e:  # noqa: BLE001
                log.warning("list_latest failed station=%s err=%s", station_id, e)
                queue = []
            last_refresh = now
            stats.loops += 1
            log.info(
                "station=%s queued %d clips (loop=%d)",
                station_id,
                len(queue),
                stats.loops,
            )
        if not queue:
            # No data for this station yet. Don't spin — sleep a clip
            # period and retry. This is normal for stations with no
            # recent activity in the source bucket.
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=CLIP_SECONDS)
                return
            except asyncio.TimeoutError:
                continue

        clip = queue.pop(0)
        cycle_start = time.monotonic()
        sequence += 1
        try:
            wav_bytes = await asyncio.to_thread(source.download, clip.blob_name)
            payload, sr = await asyncio.to_thread(
                to_pipeline_payload, wav_bytes, CODEC
            )
            ok, n_bytes = await asyncio.to_thread(
                stream_one_clip,
                station_id=station_id,
                payload=payload,
                sample_rate_hz=sr,
                sequence_number=sequence,
                codec=CODEC,
            )
            if ok:
                stats.clips_sent += 1
                stats.bytes_sent += n_bytes
                stats.last_clip_blob = clip.blob_name
                stats.last_sent_at = time.time()
            else:
                stats.clips_failed += 1
        except Exception as e:  # noqa: BLE001
            stats.clips_failed += 1
            log.warning(
                "clip_failed station=%s blob=%s err=%s",
                station_id,
                clip.blob_name,
                e,
            )

        # Pace to real time: one clip per CLIP_SECONDS wall clock.
        elapsed = time.monotonic() - cycle_start
        remaining = CLIP_SECONDS - elapsed
        if remaining > 0:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=remaining)
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
        log.info(
            "report: %d stations  clips=%d fail=%d bytes=%.1f MB",
            len(stats),
            total_sent,
            total_failed,
            total_bytes / 1e6,
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
        "argos-bridge starting  gateway=%s tls=%s codec=%s stations=%d window=%dh",
        GATEWAY_URL,
        GATEWAY_TLS,
        CODEC,
        len(station_ids),
        WINDOW_HOURS,
    )

    source = GcsClipSource(GCS_BUCKET, GCS_PREFIX)
    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    stats = [StationStats(station_id=sid) for sid in station_ids]
    tasks = [
        asyncio.create_task(replay_station(sid, source, shutdown, s))
        for sid, s in zip(station_ids, stats)
    ]
    tasks.append(asyncio.create_task(report_loop(stats, shutdown)))

    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("argos-bridge exiting")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
