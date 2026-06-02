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
  Each station pulls the current 1-second frame at its own cadence,
  encodes per its codec (WAV via libsndfile RIFF, FLAC via libsndfile
  FLAC stream, raw PCM16 when codec=""), and streams via the proto
  AudioFrame.codec field added in commit c454345.
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

# Each entry: (device_id, cadence_seconds, codec, latitude, longitude).
# The first 10 device IDs map to the already-provisioned simulator
# phones in argosuat (same public keys, etc.). Codec must match what
# the inference worker accepts ("wav", "flac", "pcm16" / "" for raw).
# Lat/lon distribute the 10 stations along the Atlantic coast of
# Patrick SFB (Cocoa Beach, FL): a ~5 km north-south line from the
# southern base perimeter (28.215 N) to the northern perimeter
# (28.260 N), all on the shoreline at ~-80.6005 W. The replay
# harness includes location in every handshake so the gateway/
# Firestore admin map reflect coast positions, not the prior
# Palm Beach test fixtures.
STATIONS_DEFAULT: tuple[tuple[str, int, str, float, float], ...] = (
    # 5 stations at 30 s (3 wav, 2 flac)
    ("DRONE-SENSOR-001", 30, "wav",  28.2150, -80.6005),  # south end
    ("DRONE-SENSOR-002", 30, "wav",  28.2200, -80.6005),
    ("DRONE-SENSOR-003", 30, "wav",  28.2250, -80.6005),
    ("DRONE-SENSOR-004", 30, "flac", 28.2300, -80.6005),
    ("DRONE-SENSOR-005", 30, "flac", 28.2350, -80.6005),  # Patrick center
    # 4 stations at 5 s (2 wav, 2 flac)
    ("DRONE-SENSOR-006",  5, "wav",  28.2400, -80.6005),
    ("DRONE-SENSOR-007",  5, "wav",  28.2450, -80.6005),
    ("DRONE-SENSOR-008",  5, "flac", 28.2500, -80.6005),
    ("DRONE-SENSOR-009",  5, "flac", 28.2550, -80.6005),
    # 1 station at 1 s (flac — the heaviest feed lands on the
    # compressed path so the FLAC decode sees the highest frame rate)
    ("DRONE-SENSOR-010",  1, "flac", 28.2600, -80.6005),  # north end
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_CLIP_PATH_DEFAULT = _REPO_ROOT / "data/test_clips/drone_flyby_test_16k_mono.wav"
GROUND_TRUTH_PATH_DEFAULT = _REPO_ROOT / "data/test_clips/ground_truth.json"

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

    def slice_1s(self, wall_ts: Optional[float] = None) -> np.ndarray:
        """Return a 1-sec PCM16 slice from the looped clip aligned to
        the playback position at wall_ts. Wraps around the loop boundary."""
        if wall_ts is None:
            wall_ts = time.time()
        pos_s = self.position_s(wall_ts)
        start_idx = int(pos_s * self.sr)
        end_idx = start_idx + self.sr
        if end_idx <= self.total_samples:
            return self.audio[start_idx:end_idx]
        first = self.audio[start_idx:]
        second = self.audio[: end_idx - self.total_samples]
        return np.concatenate([first, second])


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def stream_one_frame(
    *,
    gateway_target: str,
    use_tls: bool,
    config: StationConfig,
    samples_int16: np.ndarray,
    sample_rate_hz: int,
    sequence_number: int,
) -> tuple[bool, int, int]:
    """Open a fresh gRPC stream, push handshake + one audio frame, close.
    Returns ``(ok, payload_bytes, encode_us)``.

    Per-frame streams (vs one long-lived stream per station) keep the
    gateway's per-stream state simple and match the existing simulator's
    cycle pattern. At max cadence (1 Hz) the overhead is modest."""
    import grpc
    import drone_audio_pb2 as pb
    import drone_audio_pb2_grpc as pb_grpc

    payload, encode_us = encode_frame(samples_int16, sample_rate_hz, config.codec)

    if use_tls:
        channel = grpc.secure_channel(
            gateway_target, grpc.ssl_channel_credentials()
        )
    else:
        channel = grpc.insecure_channel(gateway_target)

    try:
        stub = pb_grpc.DroneAudioStreamStub(channel)

        # The cadence/codec tag goes in assigned_site_label so it
        # surfaces in the Site column of the admin UI and on every
        # detection event downstream — that's the field the admin
        # dashboard renders prominently per detection row.
        site_tag = f"{config.cadence_s:02d}s/{config.codec}"

        def request_iter():
            yield pb.ClientStreamMessage(
                handshake=pb.ConnectHandshake(
                    device_id=config.device_id,
                    connect_timestamp_ms=now_ms(),
                    app_version="replay-fleet-1.0",
                    device_model="replay-fleet",
                    os_version="harness",
                    assigned_site_label=site_tag,
                    # Lat/lon goes on every handshake so the gateway
                    # persists `current_location` and the admin map
                    # reflects the Patrick SFB coast layout, not stale
                    # Palm Beach test fixtures.
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
                    frame_duration_ms=1000,
                )
            )
            yield pb.ClientStreamMessage(
                audio_frame=pb.AudioFrame(
                    device_id=config.device_id,
                    capture_timestamp_ms=now_ms(),
                    sequence_number=sequence_number,
                    sample_rate_hz=sample_rate_hz,
                    pcm16_mono=payload,
                    codec=config.codec,
                )
            )

        for _resp in stub.StreamAudio(request_iter(), timeout=30):
            pass
        return True, len(payload), encode_us
    except Exception:
        return False, len(payload), encode_us
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# Per-station replay thread
# ---------------------------------------------------------------------------

def replay_station(
    config: StationConfig,
    stats: StationStats,
    gateway_target: str,
    use_tls: bool,
    clock: PlaybackClock,
    stop_event: threading.Event,
    stats_lock: threading.Lock,
) -> None:
    """Random phase offset, then send one frame per cadence_s until
    stop_event is set."""
    offset = random.uniform(0.0, config.cadence_s)
    stop_event.wait(timeout=offset)
    if stop_event.is_set():
        return

    sequence = 0
    while not stop_event.is_set():
        cycle_start = time.monotonic()
        sequence += 1
        samples = clock.slice_1s()
        ok, n_bytes, encode_us = stream_one_frame(
            gateway_target=gateway_target,
            use_tls=use_tls,
            config=config,
            samples_int16=samples,
            sample_rate_hz=clock.sr,
            sequence_number=sequence,
        )
        with stats_lock:
            if ok:
                stats.frames_sent += 1
            else:
                stats.frames_failed += 1
            stats.bytes_sent += n_bytes
            stats.encode_time_us.append(encode_us)

        elapsed = time.monotonic() - cycle_start
        remaining = config.cadence_s - elapsed
        if remaining > 0:
            stop_event.wait(timeout=remaining)


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
                   default=GROUND_TRUTH_PATH_DEFAULT,
                   help=f"Ground-truth JSON "
                        f"(default {GROUND_TRUTH_PATH_DEFAULT}).")
    p.add_argument("--duration", type=float, default=None,
                   help="Cap runtime in seconds (default: run forever).")
    p.add_argument("--report-interval", type=float, default=300.0,
                   help="Rolling summary every N seconds (default 300).")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not args.clip.is_file():
        raise SystemExit(
            f"Test clip not found: {args.clip}\n"
            f"  Place a 16 kHz mono PCM16 WAV there, or pass --clip <path>."
        )
    if not args.ground_truth.is_file():
        raise SystemExit(
            f"Ground truth not found: {args.ground_truth}\n"
            f"  Provide a JSON with 'flybys' entries or per-second labels."
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

    flybys_in_clip = load_ground_truth(args.ground_truth)
    print(f"Flybys in clip (start_s, end_s, cpa_s): {flybys_in_clip}")

    clip_duration_s = len(audio) / sr
    clock = PlaybackClock(audio, sr)
    harness_start_ts = clock.t0
    start_ts_ms = int(harness_start_ts * 1000)

    stations: dict[str, StationStats] = {}
    for did, cad, codec, lat, lon in STATIONS_DEFAULT:
        cfg = StationConfig(
            device_id=did, cadence_s=cad, codec=codec,
            latitude=lat, longitude=lon,
        )
        stations[did] = StationStats(config=cfg)
    print(f"Fleet: {len(stations)} stations")
    for s in stations.values():
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
