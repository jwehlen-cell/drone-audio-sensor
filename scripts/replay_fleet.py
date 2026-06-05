#!/usr/bin/env python3
"""Replay-fleet harness: stream a test WAV clip across the existing 10
DRONE-SENSOR phones at mixed cadences + codecs through the live
gateway -> Redis -> inference path, with detection feedback collected
from Firestore.

Design contract
---------------
* Reuses the 10 already-registered devices (DRONE-SENSOR-001..010 in
  argosuat Firestore + matching keypairs in .simulator-keys/).
* Each device_id is tagged with (cadence_seconds, codec) via the
  STATIONS_DEFAULT config table below. Edit that table to reshape the
  fleet; the harness derives a display label like
  ``DRONE-SENSOR-001 [30s/wav]`` for reports so you can tell at a
  glance which device is which configuration.
* A single ``PlaybackClock`` loops the test clip end-to-end forever.
  Each station pulls a ``cadence_s``-long slice (so a 30 s station
  sends 30 s of audio every 30 s, a 5 s station sends 5 s every 5 s,
  a 1 s station sends 1 s every second), encodes per its codec (WAV
  via libsndfile RIFF, FLAC via libsndfile FLAC stream, raw PCM16
  when codec=""), and streams via the proto AudioFrame.codec field
  added in commit c454345.
* Random phase offset within the first cadence per station so they
  aren't sample-aligned.
* Detection feedback: a separate thread polls the Firestore
  ``detections`` collection every 5 s, filters to docs published since
  the harness started AND whose device_id matches one of our stations,
  and appends them to per-station stats for the rolling report.
* Continuous by default. ``--duration N`` caps total runtime; SIGINT
  and SIGTERM stop cleanly, print the final summary, and exit.

NOT a model evaluation
----------------------
This harness measures the PIPELINE under different traffic patterns.
The YAMNet model and the 0.5 detection threshold are unchanged; the
purpose is to watch how cadence (30s vs 5s vs 1s) and codec (wav vs
flac) affect detection rate, catch rate, latency, and CPU/byte costs
at fleet scale.

Important operational note
--------------------------
Running this script while the VM-hosted simulator service is also
streaming would produce conflicting frames under the same device IDs.
Stop the simulator service on drone-sim-sender first, OR provision the
VM to launch this script instead of simulate_soh_phones.py.
"""

from __future__ import annotations

import argparse
import io
import json
import queue
import random
import signal
import statistics
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

# Reuse the simulator's proto-stub bootstrap + gateway target parser.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_soh_phones import (  # noqa: E402
    _ensure_pb_stubs,
    _parse_gateway_target,
    AUDIO_BURST_SAMPLE_RATE,
    now_ms,
)


# ---------------------------------------------------------------------------
# Configuration — edit this table to reshape the fleet
# ---------------------------------------------------------------------------

# Each entry: (device_id, cadence_seconds, codec, latitude, longitude,
#              description).
#
# Device IDs carry a SIM- prefix so operators can tell at a glance
# they're not real phones. The description is a sentence that the
# admin's Site column renders verbatim, giving each row a clear
# place-name + coordinate rather than just an opaque station id.
#
# Locations: a ~5 km north-south line from the Patrick SFB southern
# perimeter (28.2150 N) to the northern perimeter (28.2600 N), all
# on the shoreline at ~-80.6005 W. The harness puts location on every
# handshake so the gateway and admin map reflect this layout.
STATIONS_DEFAULT: tuple[tuple[str, int, str, float, float, str], ...] = (
    # 5 stations at 30 s (3 wav, 2 flac)
    ("SIM-PATRICK-001", 30, "wav",  28.2150, -80.6005,
     "SIMULATED – Patrick SFB coast, south perimeter (28.215°N)"),
    ("SIM-PATRICK-002", 30, "wav",  28.2200, -80.6005,
     "SIMULATED – Patrick SFB coast, south flank (28.220°N)"),
    ("SIM-PATRICK-003", 30, "wav",  28.2250, -80.6005,
     "SIMULATED – Patrick SFB coast, south of center (28.225°N)"),
    ("SIM-PATRICK-004", 30, "flac", 28.2300, -80.6005,
     "SIMULATED – Patrick SFB coast, just south of center (28.230°N)"),
    ("SIM-PATRICK-005", 30, "flac", 28.2350, -80.6005,
     "SIMULATED – Patrick SFB coast, center (28.235°N)"),
    # 4 stations at 5 s (2 wav, 2 flac)
    ("SIM-PATRICK-006",  5, "wav",  28.2400, -80.6005,
     "SIMULATED – Patrick SFB coast, just north of center (28.240°N)"),
    ("SIM-PATRICK-007",  5, "wav",  28.2450, -80.6005,
     "SIMULATED – Patrick SFB coast, north of center (28.245°N)"),
    ("SIM-PATRICK-008",  5, "flac", 28.2500, -80.6005,
     "SIMULATED – Patrick SFB coast, north flank (28.250°N)"),
    ("SIM-PATRICK-009",  5, "flac", 28.2550, -80.6005,
     "SIMULATED – Patrick SFB coast, near north perimeter (28.255°N)"),
    # 1 station at 1 s (flac — heaviest feed on the compressed path)
    ("SIM-PATRICK-010",  1, "flac", 28.2600, -80.6005,
     "SIMULATED – Patrick SFB coast, north perimeter (28.260°N)"),
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_CLIP_PATH_DEFAULT = _REPO_ROOT / "data/test_clips/drone_flyby_test_16k_mono.wav"
GROUND_TRUTH_PATH_DEFAULT = _REPO_ROOT / "data/test_clips/ground_truth.json"


# Multi-base fleet generator. Each base matches a SIM-{CALLSIGN}-###
# prefix in seed_test_bases.py — same lat/lon, scatter radius, and
# rng seed so the simulator phones land where the Firestore device
# docs were seeded. Patrick + Shaw are listed so the harness can opt
# into them for spot tests; the default 1,000-phone test passes the
# ten remaining bases via --bases and skips Patrick (real PHONE-*
# device) + Shaw (live ARGOS-* sensors).
_LOAD_BASES: tuple[tuple[str, str, float, float], ...] = (
    # (site_key, callsign, center_lat, center_lon)
    ("Patrick",         "PATRICK",    28.235,   -80.6005),
    ("Shaw",            "SHAW",       33.971,   -80.461),
    ("Langley",         "LANGLEY",    37.0833,  -76.3603),
    ("Vandenberg",      "VANDENBERG", 34.7420, -120.5724),
    ("Nellis",          "NELLIS",     36.2356, -115.0344),
    ("Hickam",          "HICKAM",     21.3286, -157.9472),
    ("WrightPatterson", "WPAFB",      39.8138,  -84.0494),
    ("Eielson",         "EIELSON",    64.6657, -147.0961),
    ("Andersen",        "ANDERSEN",   13.5800,  144.9244),
    ("Kadena",          "KADENA",     26.3556,  127.7676),
    ("Ramstein",        "RAMSTEIN",   49.4369,    7.6003),
    ("Buckley",         "BUCKLEY",    39.7167, -104.7517),
)
_LOAD_SCATTER_RADIUS_KM = 1.5
_LOAD_SEED = 42  # matches seed_test_bases.py for placement consistency


def _scatter(center_lat: float, center_lon: float,
             radius_km: float, rng: random.Random) -> tuple[float, float]:
    """Mirror of seed_test_bases.scatter so the simulator phones land
    on the same coordinates the Firestore device docs were seeded with."""
    import math
    r = radius_km * math.sqrt(rng.random())
    theta = rng.uniform(0, 2 * math.pi)
    dlat = (r / 111.0) * math.cos(theta)
    dlon = (r / (111.0 * math.cos(math.radians(center_lat)))) * math.sin(theta)
    return center_lat + dlat, center_lon + dlon


def build_load_test_stations(
    base_keys: list[str], phones_per_base: int, cadence_s: int, codec: str,
) -> tuple[tuple[str, int, str, float, float, str], ...]:
    """Generate a SIM-{CALLSIGN}-### station fleet for the 500-phone
    load test. Same (callsign, scatter, seed) as seed_test_bases.py so
    the simulator's handshake-emitted lat/lon lines up with the
    pre-seeded Firestore docs."""
    by_key = {b[0]: b for b in _LOAD_BASES}
    bases = [by_key[k] for k in base_keys if k in by_key]
    if not bases:
        raise SystemExit(
            f"--bases listed no known base. Known: {[b[0] for b in _LOAD_BASES]}"
        )
    rng = random.Random(_LOAD_SEED)
    out: list[tuple[str, int, str, float, float, str]] = []
    for site_key, callsign, center_lat, center_lon in bases:
        for i in range(1, phones_per_base + 1):
            lat, lon = _scatter(center_lat, center_lon,
                                _LOAD_SCATTER_RADIUS_KM, rng)
            did = f"SIM-{callsign}-{i:03d}"
            desc = (
                f"SIMULATED – {site_key} load-test phone "
                f"({lat:.4f}, {lon:.4f})"
            )
            out.append((did, cadence_s, codec, lat, lon, desc))
    return tuple(out)

# Firestore polling cadence. Tradeoff: faster polling sees detections
# sooner for the rolling report; slower means fewer Firestore reads.
DETECTION_POLL_INTERVAL_S = 5.0

# Cap unbounded per-station accumulators to avoid memory growth over
# very long runs. Detections list is uncapped (it's small).
ENCODE_TIME_RING_SIZE = 1000


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StationConfig:
    device_id: str
    cadence_s: int
    codec: str
    latitude: float
    longitude: float
    description: str = ""

    def cadence_group(self) -> str:
        return f"{self.cadence_s:02d}s"

    def label(self) -> str:
        """Display label combining device id + config tag, for reports."""
        return f"{self.device_id} [{self.cadence_s:02d}s/{self.codec}]"


@dataclass
class StationStats:
    config: StationConfig
    frames_sent: int = 0
    frames_failed: int = 0
    bytes_sent: int = 0
    encode_time_us: deque = field(
        default_factory=lambda: deque(maxlen=ENCODE_TIME_RING_SIZE)
    )
    detections: list = field(default_factory=list)  # DetectionEvent


@dataclass
class DetectionEvent:
    device_id: str
    published_at_ms: int
    peak_score: float
    category: str = ""
    subtype_label: str = ""


@dataclass
class FlybyWindow:
    """One looped occurrence of the drone flyby in absolute wall time."""
    start_ts: float
    end_ts: float
    cpa_ts: float
    loop_index: int


# ---------------------------------------------------------------------------
# Audio encoding
# ---------------------------------------------------------------------------

def encode_frame(
    samples_int16: np.ndarray, sample_rate_hz: int, codec: str
) -> tuple[bytes, int]:
    """Encode a 1-D int16 mono array per codec. Returns
    ``(payload_bytes, encode_time_us)``."""
    t0 = time.perf_counter_ns()
    if codec == "wav":
        buf = io.BytesIO()
        sf.write(buf, samples_int16, sample_rate_hz,
                 format="WAV", subtype="PCM_16")
        payload = buf.getvalue()
    elif codec == "flac":
        buf = io.BytesIO()
        sf.write(buf, samples_int16, sample_rate_hz,
                 format="FLAC", subtype="PCM_16")
        payload = buf.getvalue()
    elif codec in ("pcm16", ""):
        payload = samples_int16.tobytes()
    else:
        raise ValueError(f"unsupported codec: {codec!r}")
    encode_us = (time.perf_counter_ns() - t0) // 1000
    return payload, encode_us


# ---------------------------------------------------------------------------
# Playback clock + clip slicing
# ---------------------------------------------------------------------------

class PlaybackClock:
    """Maps wall-clock time onto a continuously looped test clip."""

    def __init__(self, audio_int16: np.ndarray, sample_rate_hz: int) -> None:
        if audio_int16.ndim > 1:
            audio_int16 = audio_int16.mean(axis=1).astype(np.int16)
        self.audio = audio_int16
        self.sr = sample_rate_hz
        self.total_samples = audio_int16.size
        self.duration_s = self.total_samples / sample_rate_hz
        # Tied to wall clock (time.time, not monotonic) so flyby
        # latencies line up with Firestore published_at_ms.
        self.t0 = time.time()

    def position_s(self, wall_ts: Optional[float] = None) -> float:
        if wall_ts is None:
            wall_ts = time.time()
        return (wall_ts - self.t0) % self.duration_s

    def slice(
        self, duration_s: float, wall_ts: Optional[float] = None
    ) -> np.ndarray:
        """Return a ``duration_s``-long PCM16 slice from the looped
        clip aligned to the playback position at ``wall_ts``. Wraps
        around the loop boundary; if ``duration_s`` exceeds one full
        loop, the loop is traversed multiple times so the caller still
        gets exactly ``duration_s * sr`` samples back."""
        if wall_ts is None:
            wall_ts = time.time()
        n_samples = int(duration_s * self.sr)
        pos_s = self.position_s(wall_ts)
        start_idx = int(pos_s * self.sr)
        end_idx = start_idx + n_samples
        if end_idx <= self.total_samples:
            return self.audio[start_idx:end_idx]
        # Wrap (and re-wrap if the request is longer than the loop).
        chunks = [self.audio[start_idx:]]
        needed = n_samples - chunks[0].size
        while needed >= self.total_samples:
            chunks.append(self.audio)
            needed -= self.total_samples
        if needed > 0:
            chunks.append(self.audio[:needed])
        return np.concatenate(chunks)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _build_handshake_msg(
    config: StationConfig,
    sample_rate_hz: int,
    battery_percent: int,
    network_type_enum: int,
):
    """Build the ConnectHandshake message that opens a persistent stream
    (or, for the legacy per-frame mode, prefixes every audio frame)."""
    import drone_audio_pb2 as pb  # type: ignore

    _name = config.description or config.device_id
    site_tag = f"{_name} ({config.cadence_s:02d}s/{config.codec})"
    return pb.ClientStreamMessage(
        handshake=pb.ConnectHandshake(
            device_id=config.device_id,
            connect_timestamp_ms=now_ms(),
            app_version="replay-fleet-1.0",
            device_model="replay-fleet",
            os_version="harness",
            assigned_site_label=site_tag,
            # Lat/lon goes on every handshake so the gateway persists
            # `current_location` and the admin map reflects the
            # configured site layout, not stale fixtures.
            location=pb.DeviceLocation(
                latitude=config.latitude,
                longitude=config.longitude,
                horizontal_accuracy_meters=5.0,
                location_timestamp_ms=now_ms(),
                provider="simulator",
                status=pb.LOCATION_STATUS_CURRENT,
            ),
            auth_token_id="simulator",
            sample_rate_hz=sample_rate_hz,
            frame_duration_ms=config.cadence_s * 1000,
            health=pb.DeviceHealth(
                battery_percent=battery_percent,
                charging=False,
                network_type=network_type_enum,
                thermal_state=pb.THERMAL_STATE_NOMINAL,
                app_version="replay-fleet-1.0",
                microphone_active=True,
            ),
        )
    )


def _build_audio_frame_msg(
    config: StationConfig,
    payload: bytes,
    sample_rate_hz: int,
    sequence_number: int,
):
    import drone_audio_pb2 as pb  # type: ignore

    return pb.ClientStreamMessage(
        audio_frame=pb.AudioFrame(
            device_id=config.device_id,
            capture_timestamp_ms=now_ms(),
            sequence_number=sequence_number,
            sample_rate_hz=sample_rate_hz,
            pcm16_mono=payload,
            codec=config.codec,
        )
    )


def _open_channel(gateway_target: str, use_tls: bool):
    import grpc  # type: ignore

    if use_tls:
        return grpc.secure_channel(
            gateway_target, grpc.ssl_channel_credentials()
        )
    return grpc.insecure_channel(gateway_target)


# Sentinel pushed into the queue to tell the request_iter generator to
# finish (and the bidirectional stream to half-close cleanly).
_END_OF_STREAM = object()


def persistent_stream_session(
    *,
    gateway_target: str,
    use_tls: bool,
    config: StationConfig,
    clock: PlaybackClock,
    stop_event: threading.Event,
    stats: StationStats,
    stats_lock: threading.Lock,
    battery_initial: int,
    battery_started_at: float,
    network_type_enum: int,
    playback_phase_s: float,
    sequence_state: list[int],
    soh_heartbeat_s: float,
) -> None:
    """Drive one long-lived bidirectional StreamAudio call.

    Opens a single gRPC channel + stream, sends the handshake once at
    start, then pushes one audio frame per cadence tick into a bounded
    queue that the request iterator drains. Periodically re-sends the
    handshake to refresh battery + location.

    Returns when (a) ``stop_event`` is set or (b) the stream errors --
    the outer ``replay_station`` reconnects in case (b).

    This matches the production phone-app pattern (open once, stream
    continuously). The legacy `per-frame stream` mode opened+closed a
    fresh gRPC channel for every audio frame, paying TCP+TLS+HTTP2+gRPC
    handshake cost once per frame -- realistic for testing per-stream
    overhead in isolation but a wildly inflated cost at high cadence
    vs what real phones do.
    """
    import grpc  # type: ignore
    import drone_audio_pb2_grpc as pb_grpc  # type: ignore

    # Bounded queue so backpressure surfaces: if the stream wedges and
    # the request iterator stops draining, queue.put eventually blocks
    # past the cadence timeout and we break out to reconnect. Capacity
    # 4 keeps the producer ~one cadence ahead at most.
    send_q: queue.Queue = queue.Queue(maxsize=4)

    def request_iter():
        # Drains messages produced by the cadence loop. Returning here
        # makes gRPC half-close the request side; the response side
        # ends naturally when the gateway closes.
        while True:
            item = send_q.get()
            if item is _END_OF_STREAM:
                return
            yield item

    channel = _open_channel(gateway_target, use_tls)
    encode_us_acc = 0
    last_handshake_at = time.monotonic()

    def _current_battery() -> int:
        drain = int((time.monotonic() - battery_started_at) / 60.0)
        return max(5, battery_initial - drain)

    # Seed the queue with the initial handshake BEFORE we hand the
    # generator to gRPC, so the gateway sees the handshake first.
    send_q.put(_build_handshake_msg(
        config, clock.sr, _current_battery(), network_type_enum,
    ))

    try:
        stub = pb_grpc.DroneAudioStreamStub(channel)
        # `timeout=None` for an indefinitely long stream; the outer
        # loop drives termination via stop_event + _END_OF_STREAM.
        response_call = stub.StreamAudio(request_iter(), timeout=None)

        # Drain server responses on a side thread so HTTP/2 flow
        # control stays healthy and the gateway can send commands
        # without blocking on us.
        def _drain_responses():
            try:
                for _resp in response_call:
                    pass
            except Exception:  # noqa: BLE001
                pass

        drainer = threading.Thread(
            target=_drain_responses,
            name=f"drain-{config.device_id}",
            daemon=True,
        )
        drainer.start()

        cycle_start = time.monotonic()
        while not stop_event.is_set():
            sequence_state[0] += 1
            samples = clock.slice(
                float(config.cadence_s),
                wall_ts=time.time() + playback_phase_s,
            )
            payload, encode_us = encode_frame(
                samples, clock.sr, config.codec,
            )
            encode_us_acc = encode_us

            frame_msg = _build_audio_frame_msg(
                config, payload, clock.sr, sequence_state[0],
            )

            # Backpressure: if the stream's wedged and the queue's
            # full for longer than a cadence cycle, give up and let
            # the outer loop reconnect.
            try:
                send_q.put(frame_msg, timeout=float(config.cadence_s))
            except queue.Full:
                with stats_lock:
                    stats.frames_failed += 1
                break

            # Optimistic per-frame stats: counted as sent when the
            # frame leaves our process via the queue. The persistent
            # stream model doesn't give us a per-frame ack, so this
            # is the closest analog to the legacy per-frame "ok".
            with stats_lock:
                stats.frames_sent += 1
                stats.bytes_sent += len(payload)
                stats.encode_time_us.append(encode_us_acc)

            # Periodic handshake refresh so the dashboard's battery /
            # site label / location stay current as the session ages.
            # Disabled when soh_heartbeat_s <= 0.
            if soh_heartbeat_s > 0 and (
                time.monotonic() - last_handshake_at >= soh_heartbeat_s
            ):
                try:
                    send_q.put(_build_handshake_msg(
                        config, clock.sr,
                        _current_battery(), network_type_enum,
                    ), timeout=1.0)
                    last_handshake_at = time.monotonic()
                except queue.Full:
                    pass  # next cycle will retry

            elapsed = time.monotonic() - cycle_start
            remaining = config.cadence_s - elapsed
            if remaining > 0:
                stop_event.wait(timeout=remaining)
            cycle_start = time.monotonic()

        # Clean shutdown: half-close so gRPC flushes anything queued.
        send_q.put(_END_OF_STREAM)
    except grpc.RpcError:
        # Stream died (network blip, gateway scale-down, etc.). The
        # outer loop handles reconnect.
        with stats_lock:
            stats.frames_failed += 1
        try:
            send_q.put_nowait(_END_OF_STREAM)
        except queue.Full:
            pass
    finally:
        try:
            channel.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Per-station replay thread
# ---------------------------------------------------------------------------

RECONNECT_BACKOFF_SECONDS = 2.0
DEFAULT_SOH_HEARTBEAT_SECONDS = 60.0


def replay_station(
    config: StationConfig,
    stats: StationStats,
    gateway_target: str,
    use_tls: bool,
    clock: PlaybackClock,
    stop_event: threading.Event,
    stats_lock: threading.Lock,
    soh_heartbeat_s: float = DEFAULT_SOH_HEARTBEAT_SECONDS,
) -> None:
    """Persistent-stream replay loop for one simulated phone.

    Matches the production phone-app pattern: open a single
    bidirectional gRPC stream and keep it alive for the lifetime of
    the simulated session. The handshake is sent once at connect; an
    audio frame is pushed every ``config.cadence_s``. If the stream
    breaks (network blip, gateway scale-down), this loop reconnects
    with a brief backoff and the SOH state is preserved across
    reconnects.

    ``soh_heartbeat_s`` controls how often a refreshed handshake is
    sent within an active stream so the dashboard's battery + site
    label + location stay current; set to 0 to disable.
    """
    offset = random.uniform(0.0, config.cadence_s)
    stop_event.wait(timeout=offset)
    if stop_event.is_set():
        return

    # Sticky per-station SOH state -- chosen once, kept across
    # reconnects so the admin dashboard's battery / network don't
    # flap when a stream restarts.
    battery_initial = random.randint(45, 95)
    network_type_enum = random.choice([1, 2])  # WIFI / CELLULAR_LTE
    battery_started_at = time.monotonic()

    # Per-station playback phase across the full clip duration so
    # stations don't all hit the same embedded flyby at the same
    # wall-clock instant (which would create one synchronized burst
    # of detections per loop).
    playback_phase_s = random.uniform(0.0, clock.duration_s)

    # Sequence number persists across reconnects so the gateway sees
    # monotonically increasing seq even after a session blip.
    sequence_state = [0]

    while not stop_event.is_set():
        persistent_stream_session(
            gateway_target=gateway_target,
            use_tls=use_tls,
            config=config,
            clock=clock,
            stop_event=stop_event,
            stats=stats,
            stats_lock=stats_lock,
            battery_initial=battery_initial,
            battery_started_at=battery_started_at,
            network_type_enum=network_type_enum,
            playback_phase_s=playback_phase_s,
            sequence_state=sequence_state,
            soh_heartbeat_s=soh_heartbeat_s,
        )
        # Brief backoff before reconnecting after a stream error.
        # If stop_event fired during the session, this returns
        # immediately and the outer while breaks.
        if not stop_event.is_set():
            stop_event.wait(timeout=RECONNECT_BACKOFF_SECONDS)


# ---------------------------------------------------------------------------
# Detection collector (Firestore polling)
# ---------------------------------------------------------------------------

def detection_collector(
    project: str,
    stations: dict[str, StationStats],
    stats_lock: threading.Lock,
    stop_event: threading.Event,
    start_ts_ms: int,
) -> None:
    """Polls Firestore for new detection docs and attributes them to
    their originating station. Reads only docs newer than
    ``start_ts_ms`` AND whose device_id matches one of our stations so
    we don't accidentally count pre-existing simulator traffic.

    Degrades gracefully when GCP creds aren't available (e.g. the sim
    VM provisioned with --no-service-account --no-scopes). In that
    case the streaming half of the harness keeps running; only the
    Firestore-derived metrics (catch rate, latency, det/h) won't be
    populated. Per-station bytes/frames/encode time still accumulate
    from the streaming side and appear in the rolling report.
    """
    try:
        from google.cloud import firestore
        client = firestore.Client(project=project)
        # One throwaway query so we catch auth failures up front
        # instead of in the steady-state loop.
        client.collection("detections").limit(1).stream()
    except Exception as e:  # noqa: BLE001
        print(
            f"WARN detection_collector: GCP auth or import failed "
            f"({type(e).__name__}: {e}); skipping Firestore polling. "
            f"Streaming + per-station send stats still run; catch rate / "
            f"latency / det-per-hour columns will stay at 0.",
            file=sys.stderr, flush=True,
        )
        return

    last_published_ms = start_ts_ms
    station_ids = set(stations.keys())

    while not stop_event.is_set():
        try:
            docs = list(
                client.collection("detections")
                .order_by("published_at_ms", direction=firestore.Query.DESCENDING)
                .limit(200)
                .stream()
            )
        except Exception as e:
            print(f"WARN detection_collector: query failed: {e}",
                  file=sys.stderr, flush=True)
            stop_event.wait(timeout=DETECTION_POLL_INTERVAL_S)
            continue

        new_max_ms = last_published_ms
        new_events = []
        for d in docs:
            data = d.to_dict()
            pub = data.get("published_at_ms") or 0
            if pub <= last_published_ms:
                continue
            dev = data.get("device_id") or ""
            if dev not in station_ids:
                continue
            ev = DetectionEvent(
                device_id=dev,
                published_at_ms=pub,
                peak_score=float(data.get("peak_score") or 0.0),
                category=(data.get("category") or {}).get("token") or "",
                subtype_label=(data.get("subtype") or {}).get("label") or "",
            )
            new_events.append(ev)
            new_max_ms = max(new_max_ms, pub)

        if new_events:
            with stats_lock:
                for ev in new_events:
                    stations[ev.device_id].detections.append(ev)
            for ev in new_events:
                ts = datetime.fromtimestamp(
                    ev.published_at_ms / 1000, tz=timezone.utc
                ).strftime("%H:%M:%S")
                cfg = stations[ev.device_id].config
                print(
                    f"[{ts}] DETECTION  {cfg.label():<35}  "
                    f"peak={ev.peak_score:.3f}  cat={ev.category:<14} "
                    f"sub={ev.subtype_label}",
                    flush=True,
                )
        last_published_ms = new_max_ms
        stop_event.wait(timeout=DETECTION_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def realized_flybys(
    now_ts: float, harness_start_ts: float,
    clip_duration_s: float, flybys_in_clip: list[tuple[float, float, float]],
    eval_slop_s: float = 30.0,
) -> list[FlybyWindow]:
    """All flyby occurrences in wall-clock time whose end_ts is far
    enough in the past (``eval_slop_s`` after end) to be evaluable for
    catch rate. Avoids penalizing the harness for detections still
    in flight."""
    out: list[FlybyWindow] = []
    if not flybys_in_clip or clip_duration_s <= 0:
        return out
    elapsed = now_ts - harness_start_ts
    max_loops = int(elapsed // clip_duration_s) + 1
    for loop_i in range(max_loops):
        for start_s, end_s, cpa_s in flybys_in_clip:
            start_ts = harness_start_ts + loop_i * clip_duration_s + start_s
            end_ts = harness_start_ts + loop_i * clip_duration_s + end_s
            cpa_ts = harness_start_ts + loop_i * clip_duration_s + cpa_s
            if end_ts + eval_slop_s < now_ts:
                out.append(FlybyWindow(start_ts, end_ts, cpa_ts, loop_i))
    return out


def print_report(
    stations: dict[str, StationStats],
    flybys_in_clip: list[tuple[float, float, float]],
    clip_duration_s: float,
    harness_start_ts: float,
    stats_lock: threading.Lock,
    header: str,
) -> None:
    """Emit per-station, per-cadence, per-codec, and fleet summary."""
    now_ts = time.time()
    elapsed_s = now_ts - harness_start_ts
    elapsed_h = elapsed_s / 3600.0

    print()
    print("=" * 96)
    print(f"{header}  (elapsed: {elapsed_s:7.0f} s = {elapsed_h:.2f} h)")
    print("=" * 96)

    per_cadence: dict[str, list[StationStats]] = defaultdict(list)
    per_codec: dict[str, list[StationStats]] = defaultdict(list)

    with stats_lock:
        print(
            f"{'station':<32} {'cad':>4}  {'codec':<5}  "
            f"{'sent':>6} {'fail':>4} {'det':>4} "
            f"{'det/h':>7}  {'bytes/s':>9}  {'enc μs':>7}"
        )
        print("-" * 96)
        for did in sorted(stations.keys()):
            s = stations[did]
            n = s.frames_sent
            f = s.frames_failed
            d = len(s.detections)
            rate = d / elapsed_h if elapsed_h > 0 else 0.0
            bps = s.bytes_sent / max(elapsed_s, 1)
            enc = statistics.mean(s.encode_time_us) if s.encode_time_us else 0
            print(
                f"{s.config.label():<32} "
                f"{s.config.cadence_s:>3}s  {s.config.codec:<5}  "
                f"{n:>6} {f:>4} {d:>4} "
                f"{rate:>7.1f}  {bps:>9.0f}  {enc:>7.0f}"
            )
            per_cadence[s.config.cadence_group()].append(s)
            per_codec[s.config.codec].append(s)

        completed = realized_flybys(
            now_ts, harness_start_ts, clip_duration_s, flybys_in_clip
        )

        print()
        print(
            f"{'cadence':<10} {'#st':>4} {'avg det/h':>10}  "
            f"{'catch rate':>12} {'median latency':>16}"
        )
        print("-" * 60)
        for cad, group in sorted(per_cadence.items()):
            total_det = sum(len(s.detections) for s in group)
            avg_rate = total_det / (len(group) * elapsed_h) if elapsed_h > 0 else 0.0
            caught = 0
            evals = 0
            latencies: list[float] = []
            for s in group:
                for fb in completed:
                    evals += 1
                    hits = [
                        d for d in s.detections
                        if fb.start_ts * 1000 <= d.published_at_ms
                        <= (fb.end_ts + 30) * 1000
                    ]
                    if hits:
                        caught += 1
                        first = min(hits, key=lambda x: x.published_at_ms)
                        latencies.append(first.published_at_ms / 1000 - fb.start_ts)
            catch_rate = caught / evals if evals else 0.0
            median_lat = statistics.median(latencies) if latencies else float("nan")
            lat_str = "    n/a" if latencies == [] else f"{median_lat:>15.1f}s"
            print(
                f"{cad:<10} {len(group):>4} {avg_rate:>10.2f}  "
                f"{catch_rate*100:>11.0f}% {lat_str}"
            )

        print()
        print(
            f"{'codec':<6} {'#st':>4} {'frames':>7} {'bytes':>11}  "
            f"{'mean enc μs':>12}"
        )
        print("-" * 50)
        for codec_name, group in sorted(per_codec.items()):
            tot_frames = sum(s.frames_sent for s in group)
            tot_bytes = sum(s.bytes_sent for s in group)
            all_us = [u for s in group for u in s.encode_time_us]
            mean_us = statistics.mean(all_us) if all_us else 0
            print(
                f"{codec_name:<6} {len(group):>4} {tot_frames:>7} "
                f"{tot_bytes:>11}  {mean_us:>12.0f}"
            )

        total_det = sum(len(s.detections) for s in stations.values())
        fleet_rate = total_det / (len(stations) * elapsed_h) if elapsed_h > 0 else 0.0
        print()
        print(
            f"Fleet average:    {fleet_rate:>6.2f} det/h "
            f"({total_det} dets / {len(stations)} stations / {elapsed_h:.2f} h)"
        )
        print(f"Flybys evaluated: {len(completed)} "
              f"(slop = end + 30 s)")


# ---------------------------------------------------------------------------
# Ground truth + CLI
# ---------------------------------------------------------------------------

def load_ground_truth(path: Path) -> list[tuple[float, float, float]]:
    """Returns ``[(start_s, end_s, cpa_s), ...]`` in clip-relative seconds.

    Accepts several schemas, tried in order:

    1. ``{"flybys": [{"start_s": 167, "end_s": 192, "cpa_s": 180}, ...]}``
       — explicit multi-flyby list.

    2. ``{"drone_present_window_s": [167, 192], "flyby_cpa_second": 180}``
       — single-flyby schema produced by build_test_clip.py. CPA is
       optional; defaults to the window midpoint.

    3. ``{"per_second": [0, 0, 1, 1, ...]}`` or
       ``{"drone_present": [...]}`` or ``{"per_second_label": [...]}``
       — per-second labels (boolean OR 0/1 ints). Flybys are derived
       from contiguous runs of truthy values.
    """
    data = json.loads(path.read_text())
    flybys = data.get("flybys")
    if flybys:
        return [
            (
                float(f["start_s"]),
                float(f["end_s"]),
                float(f.get("cpa_s", (f["start_s"] + f["end_s"]) / 2)),
            )
            for f in flybys
        ]
    window = data.get("drone_present_window_s")
    if window and isinstance(window, (list, tuple)) and len(window) == 2:
        start_s, end_s = float(window[0]), float(window[1])
        cpa_s = float(data.get("flyby_cpa_second", (start_s + end_s) / 2))
        return [(start_s, end_s, cpa_s)]
    labels = (
        data.get("per_second")
        or data.get("drone_present")
        or data.get("per_second_label")
    )
    if labels and isinstance(labels, list):
        runs: list[tuple[float, float, float]] = []
        in_run = False
        run_start = 0
        for i, present in enumerate(labels):
            if present and not in_run:
                in_run = True
                run_start = i
            elif not present and in_run:
                in_run = False
                runs.append((float(run_start), float(i), (run_start + i) / 2.0))
        if in_run:
            n = len(labels)
            runs.append((float(run_start), float(n), (run_start + n) / 2.0))
        return runs
    raise ValueError(
        f"{path}: ground truth missing 'flybys' / "
        f"'drone_present_window_s' / per-second labels"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gateway-url", required=True,
                   help="gRPC host:port (use https:// prefix for TLS).")
    p.add_argument("--project", required=True,
                   help="GCP project for Firestore detection polling.")
    p.add_argument("--clip", type=Path, default=TEST_CLIP_PATH_DEFAULT,
                   help=f"Test WAV clip to loop "
                        f"(default {TEST_CLIP_PATH_DEFAULT}).")
    p.add_argument("--ground-truth", type=Path,
                   default=None,
                   help="Ground-truth JSON (defaults to <clip>.ground_truth.json "
                        "or data/test_clips/ground_truth.json if present; "
                        "omitted entirely if no file is found — catch-rate "
                        "columns then stay at 0).")
    p.add_argument("--duration", type=float, default=None,
                   help="Cap runtime in seconds (default: run forever).")
    # --- Load-test fleet generator (alternative to STATIONS_DEFAULT). ---
    # When --bases is set, replace the 10-station Patrick fleet with a
    # programmatic SIM-{CALLSIGN}-### scatter so the same harness drives
    # both the cadence/codec science test and the 500-phone load test.
    p.add_argument("--bases", default="",
                   help="Comma-separated base keys to seed stations for "
                        "(Patrick, Shaw, Langley, Vandenberg, Nellis, "
                        "Hickam, WrightPatterson). Empty = use the "
                        "hard-coded Patrick fleet.")
    p.add_argument("--phones-per-base", type=int, default=100,
                   help="Stations per base when --bases is set (default 100).")
    p.add_argument("--cadence-seconds", type=int, default=30,
                   help="Cadence applied to every load-test station (default 30).")
    p.add_argument("--codec", default="flac",
                   help="Codec applied to every load-test station "
                        "(wav|flac|pcm16; default flac).")
    p.add_argument("--report-interval", type=float, default=300.0,
                   help="Rolling summary every N seconds (default 300).")
    p.add_argument(
        "--soh-heartbeat-seconds",
        type=float,
        default=DEFAULT_SOH_HEARTBEAT_SECONDS,
        help="On each persistent stream, re-send the handshake every N "
             "seconds so the admin dashboard's battery + site label + "
             "location stay current. 0 disables refresh (handshake only "
             "at connect). Default 60.",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not args.clip.is_file():
        raise SystemExit(
            f"Test clip not found: {args.clip}\n"
            f"  Place a 16 kHz mono PCM16 WAV there, or pass --clip <path>."
        )

    _ensure_pb_stubs()
    target, use_tls = _parse_gateway_target(args.gateway_url)

    audio, sr = sf.read(str(args.clip), dtype="int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.int16)
    if sr != AUDIO_BURST_SAMPLE_RATE:
        raise SystemExit(
            f"Clip sample rate must be {AUDIO_BURST_SAMPLE_RATE} Hz; "
            f"got {sr} Hz. Re-export the clip to 16 kHz mono."
        )
    print(f"Loaded clip: {len(audio)/sr:.1f} s @ {sr} Hz "
          f"({args.clip.name})")

    # Ground truth: optional. Try explicit path, then sibling
    # <clip>.ground_truth.json, then the legacy default. Skip
    # catch-rate accounting if none is found — load tests don't
    # always have or need it.
    flybys_in_clip: list[tuple[float, float, float]] = []
    gt_candidates: list[Path] = []
    if args.ground_truth is not None:
        gt_candidates.append(args.ground_truth)
    gt_candidates.append(args.clip.with_suffix(".ground_truth.json"))
    gt_candidates.append(GROUND_TRUTH_PATH_DEFAULT)
    for cand in gt_candidates:
        if cand and cand.is_file():
            flybys_in_clip = load_ground_truth(cand)
            print(f"Loaded ground truth from {cand}: {flybys_in_clip}")
            break
    else:
        print("No ground truth available; catch-rate columns will stay at 0.")

    clip_duration_s = len(audio) / sr
    clock = PlaybackClock(audio, sr)
    harness_start_ts = clock.t0
    start_ts_ms = int(harness_start_ts * 1000)

    if args.bases.strip():
        base_keys = [b.strip() for b in args.bases.split(",") if b.strip()]
        fleet_def = build_load_test_stations(
            base_keys=base_keys,
            phones_per_base=args.phones_per_base,
            cadence_s=args.cadence_seconds,
            codec=args.codec,
        )
        print(
            f"Load-test fleet: {len(fleet_def)} stations across "
            f"{len(base_keys)} base(s) at {args.cadence_seconds}s/{args.codec}"
        )
    else:
        fleet_def = STATIONS_DEFAULT

    stations: dict[str, StationStats] = {}
    for did, cad, codec, lat, lon, desc in fleet_def:
        cfg = StationConfig(
            device_id=did, cadence_s=cad, codec=codec,
            latitude=lat, longitude=lon, description=desc,
        )
        stations[did] = StationStats(config=cfg)
    print(f"Fleet: {len(stations)} stations")
    # For large fleets, just print a sample so the systemd log doesn't drown.
    if len(stations) <= 20:
        for s in stations.values():
            print(f"  {s.config.label()}")
    else:
        sample = list(stations.values())
        for s in sample[:3]:
            print(f"  {s.config.label()}")
        print(f"  ... {len(stations) - 6} more ...")
        for s in sample[-3:]:
            print(f"  {s.config.label()}")

    stop_event = threading.Event()
    stats_lock = threading.Lock()

    def _stop(signum, _frame):
        print(f"\nReceived signal {signum}; stopping ...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    threads: list[threading.Thread] = []
    for stats in stations.values():
        t = threading.Thread(
            target=replay_station,
            args=(stats.config, stats, target, use_tls, clock, stop_event,
                  stats_lock),
            kwargs={"soh_heartbeat_s": args.soh_heartbeat_seconds},
            name=f"replay-{stats.config.device_id}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    collector = threading.Thread(
        target=detection_collector,
        args=(args.project, stations, stats_lock, stop_event, start_ts_ms),
        name="detection-collector",
        daemon=True,
    )
    collector.start()
    threads.append(collector)

    last_report = time.monotonic()
    end_time = (time.monotonic() + args.duration) if args.duration else None
    while not stop_event.is_set():
        stop_event.wait(timeout=1.0)
        now = time.monotonic()
        if now - last_report >= args.report_interval:
            now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print_report(
                stations, flybys_in_clip, clip_duration_s, harness_start_ts,
                stats_lock, header=f"Rolling summary @ {now_utc}",
            )
            last_report = now
        if end_time is not None and now >= end_time:
            print("\nDuration cap reached; stopping ...", flush=True)
            stop_event.set()

    for t in threads:
        t.join(timeout=10)

    print_report(
        stations, flybys_in_clip, clip_duration_s, harness_start_ts,
        stats_lock, header="FINAL SUMMARY",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
