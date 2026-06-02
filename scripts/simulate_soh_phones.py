#!/usr/bin/env python3
"""Keep fake phones visible on the SOH dashboard without streaming audio.

In the normal laptop mode, this posts fake phone state to the SOH/admin
Cloud Run URL. The admin service writes Firestore and private Redis from inside
the VPC connector. It does not connect to the gateway and it does not add audio
frames to the Redis stream, so it avoids the inference path entirely.

Requires:
    pip install -r scripts/requirements.txt
    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<your-project>

The direct Redis mode is still available for VPC-connected shells.

Optional `--audio-burst` flag: in addition to the heartbeat, each cycle one
random phone streams 5 seconds of synthetic drone-propeller buzz to the
gateway while the other phones stream low-amplitude white noise. This drives
the trained YAMNet+ERAU classifier and produces a single detection per cycle
on the dashboard. Requires `--gateway-url`. Heartbeat behavior is unchanged.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


DEFAULT_BASE_LAT = 38.8977
DEFAULT_BASE_LON = -77.0365
DEFAULT_INTERVAL_SECONDS = 180
DEFAULT_REDIS_TTL_SECONDS = 300

AUDIO_BURST_SAMPLE_RATE = 16_000
AUDIO_BURST_FRAME_SECONDS = 1
AUDIO_BURST_DEFAULT_FRAMES = 5  # five 1-second frames -> 5 seconds of audio
PROTO_FILE = Path(__file__).resolve().parent.parent / "proto" / "drone_audio.proto"

# Patrick Space Force Base (Cocoa Beach, FL) Atlantic coast layout.
# 10 stations distributed ~500 m apart along a ~5 km north-south line at
# the shoreline (~-80.6005 W). South end at 28.2150 N, north end at
# 28.2600 N. Replaces the prior Palm Beach test fixtures so admin map +
# device docs reflect the operational deployment context.
DEFAULT_PHONE_FIXTURES = (
    ("DRONE-SENSOR-001", 28.2150, -80.6005, "Patrick SFB coast - south boundary"),
    ("DRONE-SENSOR-002", 28.2200, -80.6005, "Patrick SFB coast - south"),
    ("DRONE-SENSOR-003", 28.2250, -80.6005, "Patrick SFB coast - south central"),
    ("DRONE-SENSOR-004", 28.2300, -80.6005, "Patrick SFB coast - mid south"),
    ("DRONE-SENSOR-005", 28.2350, -80.6005, "Patrick SFB coast - center"),
    ("DRONE-SENSOR-006", 28.2400, -80.6005, "Patrick SFB coast - mid north"),
    ("DRONE-SENSOR-007", 28.2450, -80.6005, "Patrick SFB coast - north central"),
    ("DRONE-SENSOR-008", 28.2500, -80.6005, "Patrick SFB coast - north"),
    ("DRONE-SENSOR-009", 28.2550, -80.6005, "Patrick SFB coast - north boundary"),
    ("DRONE-SENSOR-010", 28.2600, -80.6005, "Patrick SFB coast - north end"),
)


@dataclass
class LiveDeviceState:
    device_id: str
    session_id: str
    last_seen_ms: int
    last_sequence: int
    frames_received: int
    dropped_frames: int
    reconnect_count: int
    app_version: str
    network_type: str
    battery_percent: int
    thermal_state: str
    site_label: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate low-cost SOH activity for fake phones."
    )
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--firestore-database", default="(default)")
    parser.add_argument("--collection", default="devices")
    parser.add_argument(
        "--with-keypairs",
        action="store_true",
        help="Generate/load one EC P-256 keypair per simulated phone and write public keys to Firestore.",
    )
    parser.add_argument(
        "--key-dir",
        type=Path,
        default=Path(".simulator-keys"),
        help="Local directory for simulated phone private/public keys.",
    )
    parser.add_argument(
        "--admin-url",
        help="SOH/admin Cloud Run URL. Laptop mode posts simulated phones to this URL.",
    )
    parser.add_argument(
        "--simulator-token",
        default=os.environ.get("SOH_SIMULATOR_TOKEN", ""),
        help="Optional token sent as X-SOH-Simulator-Token.",
    )
    parser.add_argument("--redis-host")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-db", type=int, default=0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--prefix", default="FAKE-SOH-PHONE")
    parser.add_argument("--site", default="SOH Test")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--redis-ttl-seconds", type=int, default=DEFAULT_REDIS_TTL_SECONDS)
    parser.add_argument("--base-lat", type=float, default=DEFAULT_BASE_LAT)
    parser.add_argument("--base-lon", type=float, default=DEFAULT_BASE_LON)
    parser.add_argument(
        "--generic",
        action="store_true",
        help="Use generated FAKE-SOH-PHONE IDs instead of the default DRONE-SENSOR fixtures.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Write one refresh and exit instead of looping.",
    )
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="Write Firestore registration/location docs and exit without touching Redis.",
    )
    parser.add_argument(
        "--audio-burst",
        action="store_true",
        help=(
            "Each cycle, stream 5 seconds of synthetic audio per phone to the gateway. "
            "One random phone sends drone-propeller buzz; the rest send low-amplitude "
            "white noise. Requires --gateway-url."
        ),
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("DRONE_SENSOR_GATEWAY_URL", ""),
        help=(
            "Public gRPC URL of the gateway, e.g. "
            "drone-sensor-dev-gateway-65av54lbuq-uc.a.run.app or full https://...; "
            "used only when --audio-burst is set."
        ),
    )
    parser.add_argument(
        "--audio-burst-seconds",
        type=int,
        default=AUDIO_BURST_DEFAULT_FRAMES,
        help="Number of 1-second frames each phone streams per cycle (default 5).",
    )
    return parser.parse_args()


def now_ms() -> int:
    return int(time.time() * 1000)


def device_id(prefix: str, n: int) -> str:
    return f"{prefix}-{n:03d}"


def location(base_lat: float, base_lon: float, n: int) -> tuple[float, float]:
    # Small deterministic spread so the markers do not sit on top of each other.
    row = (n - 1) // 5
    col = (n - 1) % 5
    return base_lat + row * 0.004, base_lon + col * 0.004


def phone_fixtures(args: argparse.Namespace) -> list[tuple[str, float, float, str]]:
    if not args.generic:
        return list(DEFAULT_PHONE_FIXTURES[: args.count])
    return [
        (
            device_id(args.prefix, n),
            *location(args.base_lat, args.base_lon, n),
            args.site,
        )
        for n in range(1, args.count + 1)
    ]


def upsert_firestore_devices(
    db,
    geo_point,
    *,
    collection: str,
    phones: list[tuple[str, float, float, str]],
    timestamp_ms: int,
    public_keys: dict[str, str] | None = None,
) -> None:
    batch = db.batch()
    for did, lat, lon, site in phones:
        doc = db.collection(collection).document(did)
        payload = {
            "device_id": did,
            "state": "active",
            "assigned_site_label": site,
            "app_version": "sim-soh-1.0",
            "device_model": "SOH simulator",
            "os_version": "local-only",
            "first_seen_ms": timestamp_ms,
            "last_seen_ms": timestamp_ms,
            "last_handshake_ms": timestamp_ms,
            "current_location": geo_point(lat, lon),
            "location_accuracy_m": 5.0,
            "location_status": "current",
            "location_timestamp_ms": timestamp_ms,
            "admin_notes": "Generated by scripts/simulate_soh_phones.py",
        }
        if public_keys and did in public_keys:
            payload["public_key_pem"] = public_keys[did]
        batch.set(doc, payload, merge=True)
    batch.commit()


def ensure_keypairs(
    phones: list[tuple[str, float, float, str]],
    key_dir: Path,
) -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key_dir.mkdir(parents=True, exist_ok=True)
    public_keys: dict[str, str] = {}
    for did, _lat, _lon, _site in phones:
        private_path = key_dir / f"{did}.private.pem"
        public_path = key_dir / f"{did}.public.pem"
        if private_path.exists():
            private_key = serialization.load_pem_private_key(
                private_path.read_bytes(),
                password=None,
            )
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            private_path.write_bytes(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            private_path.chmod(0o600)

        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        public_path.write_text(public_pem)
        public_keys[did] = public_pem
    return public_keys


def refresh_redis(
    client,
    *,
    phones: list[tuple[str, float, float, str]],
    timestamp_ms: int,
    ttl_seconds: int,
    tick: int,
) -> None:
    pipe = client.pipeline(transaction=False)
    for n, (did, _lat, _lon, site) in enumerate(phones, start=1):
        state = LiveDeviceState(
            device_id=did,
            session_id=f"sim-{uuid.uuid4().hex[:12]}",
            last_seen_ms=timestamp_ms,
            last_sequence=tick,
            frames_received=tick,
            dropped_frames=0,
            reconnect_count=max(0, tick - 1),
            app_version="sim-soh-1.0",
            network_type="wifi",
            battery_percent=max(35, 96 - n),
            thermal_state="nominal",
            site_label=site,
        )
        pipe.set(f"device:{did}", json.dumps(asdict(state)), ex=ttl_seconds)
    pipe.execute()


def post_to_admin_url(
    *,
    admin_url: str,
    token: str,
    phones: list[tuple[str, float, float, str]],
    timestamp_ms: int,
    ttl_seconds: int,
    tick: int,
) -> None:
    endpoint = admin_url.rstrip("/") + "/api/simulate/phones"
    payload = {
        "timestamp_ms": timestamp_ms,
        "ttl_seconds": ttl_seconds,
        "tick": tick,
        "phones": [
            {
                "device_id": did,
                "lat": lat,
                "lon": lon,
                "site_label": site,
                "battery_percent": max(35, 96 - n),
                "network_type": "wifi",
            }
            for n, (did, lat, lon, site) in enumerate(phones, start=1)
        ],
    }
    req = urlrequest.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "drone-soh-local-simulator/1.0",
        },
    )
    if token:
        req.add_header("X-SOH-Simulator-Token", token)
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"admin API returned HTTP {resp.status}")
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"admin API returned HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"failed to reach admin API: {e}") from e


# ---------------------------------------------------------------------------
# --audio-burst: stream synthetic PCM16 frames at the gateway each cycle.
# Heartbeat-only mode never imports these — keep imports lazy inside the
# helpers so the default path stays light.
# ---------------------------------------------------------------------------


def _ensure_pb_stubs() -> Path:
    """Generate drone_audio_pb2 / _pb2_grpc into a temp dir and put it on
    sys.path. Idempotent across runs in the same shell."""
    out = Path(tempfile.gettempdir()) / "drone_audio_simulator_pb"
    out.mkdir(exist_ok=True)
    if (out / "drone_audio_pb2.py").is_file() and (out / "drone_audio_pb2_grpc.py").is_file():
        if str(out) not in sys.path:
            sys.path.insert(0, str(out))
        return out
    if not PROTO_FILE.is_file():
        raise SystemExit(f"proto file not found: {PROTO_FILE}")
    subprocess.run(
        [
            sys.executable, "-m", "grpc_tools.protoc",
            f"-I{PROTO_FILE.parent}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            str(PROTO_FILE),
        ],
        check=True,
    )
    if str(out) not in sys.path:
        sys.path.insert(0, str(out))
    return out


# Empirically tuned synthetic profiles that hit a specific subtype class on
# the trained ERAU subtype head. The parameter tuples are (fundamental Hz,
# [(harmonic_multiplier, amplitude), ...], AM rate Hz, AM depth, noise amp).
# These are not physical models of the drones; they're synthetic signals that
# happen to land in a drone-positive region of YAMNet-embedding space.
#
# Historical note: the named-subtype profiles (known-mavic3 / known-matrice /
# known-mavicmini) were tuned against the original 4-class ERAU-only model
# and at the time produced their own subtype labels (binary 0.85-1.00,
# subtype 0.44-0.82 on the named class). After the 2026-05-30 retrain
# (commit fb10801 — adds the Parrot Bebop class and ~5 k DroneAudioset
# rotor recordings), the subtype head's bebop region has absorbed
# essentially the entire simple-harmonic-stack family that this
# synthesizer produces. Empirical readback at fb10801 (5-seed average,
# 1-sec frame):
#
#   known-mavic3    => binary 1.00, subtype bebop 0.93 (was mavic3 0.82)
#   known-matrice   => binary 1.00, subtype bebop 0.99 (was matrice 0.80)
#   known-mavicmini => binary 1.00, subtype bebop 0.97 (was mavicmini 0.44)
#   known-bebop     => binary 1.00, subtype bebop 0.99 (added in this commit)
#   unknown-drone   => binary 0.98, subtype no_drone 0.74 (triggers
#                                                     "Unknown drone" in
#                                                     the admin UI)
#
# So in argosuat right now, the binary pipeline fires reliably and the
# admin UI sees Parrot Bebop + Unknown drone but not the other three
# trained subtypes. The named-subtype profiles are kept because they
# still produce drone-positive signals (useful for the binary head's
# operational telemetry) and because their names document the original
# intent, but they no longer assert what the classifier will return.
#
# A wider sweep at fb10801 found no simple-synth parameters in the
# (fundamental Hz, harmonic stack, AM rate, AM depth, noise amp) family
# that hit matrice / mavic3 / mavicmini / mambo at top-1. To exercise
# those classes end-to-end the simulator would need to switch from
# pure synthesis to streaming real WAVs (e.g. clips from
# data/extra_raw/DroneAudioDataset/Multiclass_Drone_Audio/{bebop_1,
# membo_1}/ for Parrot, or the visualization repo for DJI). That
# changeover is intentionally NOT in this commit.
_DRONE_PROFILES: tuple[tuple[str, float, list[tuple[int, float]], float, float, float], ...] = (
    ("known-mavic3",
     160.0, [(1, 0.30), (2, 0.20), (3, 0.12), (4, 0.06)], 14.0, 0.50, 0.12),
    ("known-matrice",
     280.0, [(1, 0.30), (2, 0.16), (3, 0.08)],            28.0, 0.60, 0.14),
    ("known-mavicmini",
     190.0, [(1, 0.20), (2, 0.15)],                       12.0, 0.40, 0.20),
    ("known-bebop",
     110.0, [(1, 0.30), (2, 0.22), (3, 0.14), (4, 0.08)], 13.0, 0.50, 0.12),
    ("unknown-drone",
     180.0, [(1, 0.30), (2, 0.22), (3, 0.12)],            15.0, 0.35, 0.05),
)

# How frequently each profile is chosen per drone cycle. The 4 known
# profiles get equal weight and the unknown gets 2 so it shows up
# regularly enough to exercise the "Unknown drone" category in the
# admin UI without dominating the trained-class signal.
_DRONE_PROFILE_WEIGHTS = {
    "known-mavic3":    3,
    "known-matrice":   3,
    "known-mavicmini": 3,
    "known-bebop":     3,
    "unknown-drone":   2,
}


def _pick_drone_profile() -> tuple[str, float, list[tuple[int, float]], float, float, float]:
    names = [p[0] for p in _DRONE_PROFILES]
    weights = [_DRONE_PROFILE_WEIGHTS.get(n, 1) for n in names]
    chosen = random.choices(names, weights=weights, k=1)[0]
    for prof in _DRONE_PROFILES:
        if prof[0] == chosen:
            return prof
    return _DRONE_PROFILES[0]


# Real-WAV streaming. When the sim VM startup script clones the two
# upstream audio repos under data/sim_audio_fixtures/, the simulator
# prefers playing those real recordings over the synthetic profiles
# below, because the retrained classifier collapses every parameter
# combination of the simple synthesizer onto the bebop region. With
# real audio we exercise the full label set the model actually knows
# (Parrot Bebop, Parrot Mambo) plus a steady stream of untrained DJI
# clips that surface as "Unknown drone".
#
# Each entry: (label, glob pattern relative to repo root, weight).
# The pool of (label, wav_path) tuples is built once per cycle; each
# cycle picks one weighted-random tuple, reads the WAV, and streams it
# as the burst audio.
_REPO_ROOT = Path(__file__).parent.parent
_REAL_AUDIO_SOURCES: tuple[tuple[str, str, int], ...] = (
    ("parrot-bebop",
     "data/sim_audio_fixtures/DroneAudioDataset/Multiclass_Drone_Audio/bebop_1/*.wav",
     3),
    ("parrot-mambo",
     "data/sim_audio_fixtures/DroneAudioDataset/Multiclass_Drone_Audio/membo_1/*.wav",
     3),
    ("dji-untrained",
     "data/sim_audio_fixtures/drone-visualization/public/droneAudio/DJI_*.wav",
     7),
)

# Vis-repo clips that don't trigger the production K-of-N gate (5
# frames at threshold 0.5). Excluded so the simulator never streams
# a drone that would silently miss — operator preference: never miss
# a drone, even if mis-classified. Re-run /tmp/audit_vis_dji.py after
# each retrain to refresh this list.
_VIS_REPO_SILENT_MISSES: frozenset[str] = frozenset({
    "DJI_Mini3_pro_30.wav",
})


def _list_real_wav_pool() -> dict[str, tuple[int, list[Path]]]:
    """Build ``{category_label: (weight, [wav_path, ...])}`` from on-disk
    fixtures. Empty dict = no fixtures present, falls back to synth.

    Weights are per-CATEGORY (not per-WAV), so a category with one file
    has the same chance of being picked as a category with hundreds.
    Inside a category the simulator picks a random WAV with uniform
    probability. WAVs in ``_VIS_REPO_SILENT_MISSES`` are filtered out
    so the simulator never streams a drone that wouldn't trigger the
    production K-of-N gate."""
    out: dict[str, tuple[int, list[Path]]] = {}
    for label, pattern, weight in _REAL_AUDIO_SOURCES:
        paths: list[Path] = []
        for path in sorted(_REPO_ROOT.glob(pattern)):
            if path.name in _VIS_REPO_SILENT_MISSES:
                continue
            paths.append(path)
        if paths:
            out[label] = (weight, paths)
    return out


def _pick_real_wav() -> tuple[str, Path] | None:
    pool = _list_real_wav_pool()
    if not pool:
        return None
    categories = []
    for label, (weight, _paths) in pool.items():
        categories.extend([label] * weight)
    label = random.choice(categories)
    return label, random.choice(pool[label][1])


def _read_wav_as_pcm16_frames(
    wav_path: Path, frame_seconds: int, frame_count: int
) -> list[bytes]:
    """Read a WAV file, downmix to mono, resample to 16 kHz, then return
    ``frame_count`` separate PCM16 little-endian byte buffers each
    ``frame_seconds`` long. Loops the source audio if it's shorter than
    the requested total duration; truncates if longer."""
    import soundfile as sf
    import numpy as np
    audio, sr = sf.read(str(wav_path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != AUDIO_BURST_SAMPLE_RATE:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr), AUDIO_BURST_SAMPLE_RATE)
        audio = resample_poly(
            audio, AUDIO_BURST_SAMPLE_RATE // g, int(sr) // g
        ).astype(np.float32)
    target = AUDIO_BURST_SAMPLE_RATE * frame_seconds * frame_count
    if audio.size >= target:
        audio = audio[:target]
    else:
        reps = (target + audio.size - 1) // audio.size
        audio = np.tile(audio, reps)[:target]
    audio = np.clip(audio, -1.0, 1.0)
    pcm16 = (audio * 32767).astype(np.int16).tobytes()
    chunk_bytes = AUDIO_BURST_SAMPLE_RATE * frame_seconds * 2
    return [pcm16[i * chunk_bytes : (i + 1) * chunk_bytes] for i in range(frame_count)]


def _make_drone_buzz_pcm16(seconds: int, profile: tuple | None = None) -> bytes:
    """Generate a synthetic drone-like PCM16 buffer using one of the empirically-
    tuned profiles. If a profile is not passed, picks one at random per the
    weight table above."""
    import numpy as np
    if profile is None:
        profile = _pick_drone_profile()
    _name, fund, harms, mod_hz, am_depth, noise_amp = profile
    sr = AUDIO_BURST_SAMPLE_RATE
    t = np.linspace(0, seconds, sr * seconds, endpoint=False)
    sig = np.zeros_like(t)
    for mult, amp in harms:
        sig += amp * np.sin(2 * np.pi * fund * mult * t)
    if mod_hz > 0:
        sig *= (1 - am_depth * 0.5 * (1 + np.sin(2 * np.pi * mod_hz * t)))
    sig += noise_amp * np.random.randn(t.size)
    sig = np.clip(sig * 0.7, -1.0, 1.0)
    return (sig * 32767).astype(np.int16).tobytes()


# Lossless FLAC compression of PCM16 audio frames before send. Empirical
# ratio on drone-rotor audio (1-sec frame, 16 kHz mono): ~1.26x on
# Parrot Bebop, ~1.57x on Parrot Mambo, ~1.04x on the synthetic profiles
# (low-entropy tones still need their per-sample LSB). Encode + decode
# add ~3 ms / ~1 ms each — negligible vs the YAMNet bottleneck.
#
# FLAC beats zstd here because libFLAC has a linear-prediction model
# tailored for audio: it predicts the next sample from a few prior
# samples and entropy-codes the residual. Generic byte-level
# compression can't exploit that structure (zstd at level 22 only got
# 1.06-1.42x on the same corpus).
#
# Falls back to raw PCM16 if soundfile isn't importable so the
# simulator still runs in a stripped-down checkout.
try:
    import io as _io
    import soundfile as _sf  # type: ignore[import-not-found]
    _AUDIO_CODEC = "flac"
except Exception:  # noqa: BLE001
    _sf = None
    _AUDIO_CODEC = ""


def _encode_audio_payload(pcm16: bytes) -> tuple[bytes, str]:
    """FLAC-encode raw PCM16 if soundfile is available, otherwise pass
    through unchanged.

    Returns ``(payload_bytes, codec_name)`` for the AudioFrame.codec
    proto field: ``"flac"`` or ``""`` for legacy raw PCM16.
    """
    if _sf is None:
        return pcm16, ""
    import numpy as np
    samples = np.frombuffer(pcm16, dtype=np.int16)
    buf = _io.BytesIO()
    _sf.write(buf, samples, AUDIO_BURST_SAMPLE_RATE, format="FLAC", subtype="PCM_16")
    return buf.getvalue(), _AUDIO_CODEC


def _make_noise_pcm16(seconds: int) -> bytes:
    """Pure PCM-zero silence. Any audible amplitude of random noise lands in
    a YAMNet-embedding region the trained head wasn't exposed to (the ERAU
    'no_drone' class was real cars/jets/wind, not white noise) and spuriously
    triggers the classifier; even a 0.001 dither was enough to do so in
    practice. Pure zeros produce a near-zero YAMNet embedding and cleanly
    map to drone_score ~0.0."""
    return b"\x00\x00" * (AUDIO_BURST_SAMPLE_RATE * seconds)


def _stream_audio_burst_for_phone(
    *,
    gateway_target: str,
    use_tls: bool,
    device_id: str,
    site_label: str,
    lat: float,
    lon: float,
    frame_count: int,
    is_drone: bool,
    drone_profile: tuple | None = None,
    drone_wav: Path | None = None,
) -> tuple[str, bool, str]:
    """Open a single gRPC bidi stream, push a handshake + frame_count audio
    frames, then close. Returns (device_id, ok, detail)."""
    import grpc
    import drone_audio_pb2 as pb
    import drone_audio_pb2_grpc as pb_grpc

    sr = AUDIO_BURST_SAMPLE_RATE
    frame_seconds = AUDIO_BURST_FRAME_SECONDS

    if is_drone:
        if drone_wav is not None:
            pcm_chunks = _read_wav_as_pcm16_frames(
                drone_wav, frame_seconds, frame_count
            )
        else:
            pcm_chunks = [
                _make_drone_buzz_pcm16(frame_seconds, profile=drone_profile)
                for _ in range(frame_count)
            ]
    else:
        pcm_chunks = [_make_noise_pcm16(frame_seconds) for _ in range(frame_count)]

    credentials = grpc.ssl_channel_credentials() if use_tls else None
    if use_tls:
        channel = grpc.secure_channel(gateway_target, credentials)
    else:
        channel = grpc.insecure_channel(gateway_target)

    try:
        stub = pb_grpc.DroneAudioStreamStub(channel)

        def request_iter():
            yield pb.ClientStreamMessage(
                handshake=pb.ConnectHandshake(
                    device_id=device_id,
                    connect_timestamp_ms=now_ms(),
                    app_version="sim-audio-burst-1.0",
                    device_model="SOH simulator",
                    os_version="local-only",
                    assigned_site_label=site_label,
                    location=pb.DeviceLocation(
                        latitude=lat,
                        longitude=lon,
                        horizontal_accuracy_meters=5.0,
                        location_timestamp_ms=now_ms(),
                        provider="simulator",
                        status=pb.LOCATION_STATUS_CURRENT,
                    ),
                    health=pb.DeviceHealth(
                        battery_percent=80,
                        network_type=pb.NETWORK_TYPE_WIFI,
                        app_version="sim-audio-burst-1.0",
                        thermal_state=pb.THERMAL_STATE_NOMINAL,
                        microphone_active=True,
                    ),
                    auth_token_id="simulator",
                    sample_rate_hz=sr,
                    frame_duration_ms=frame_seconds * 1000,
                )
            )
            for i, pcm in enumerate(pcm_chunks):
                payload, codec = _encode_audio_payload(pcm)
                yield pb.ClientStreamMessage(
                    audio_frame=pb.AudioFrame(
                        device_id=device_id,
                        capture_timestamp_ms=now_ms(),
                        sequence_number=i + 1,
                        sample_rate_hz=sr,
                        pcm16_mono=payload,
                        codec=codec,
                    )
                )
                # Mild pacing so the gateway's per-stream state has a chance
                # to flush each frame to Redis before the next arrives.
                time.sleep(0.05)

        # Drain server-side responses; we don't act on them but we need to
        # consume the iterator for the stream to close cleanly.
        for _resp in stub.StreamAudio(request_iter(), timeout=30):
            pass
        detail = drone_profile[0] if (is_drone and drone_profile) else (
            "drone-buzz" if is_drone else "noise"
        )
        return (device_id, True, detail)
    except Exception as e:  # noqa: BLE001
        return (device_id, False, f"{type(e).__name__}: {e}")
    finally:
        try:
            channel.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_gateway_target(url: str) -> tuple[str, bool]:
    """Accept either 'host[:port]' or 'https://host[:port]/'; return
    (host:port, use_tls)."""
    raw = url.strip().rstrip("/")
    if raw.startswith("http://"):
        host = raw[len("http://"):]
        return (host if ":" in host else f"{host}:80", False)
    if raw.startswith("https://"):
        host = raw[len("https://"):]
        return (host if ":" in host else f"{host}:443", True)
    # Bare host: default to TLS:443 (Cloud Run terminates TLS for us).
    return (raw if ":" in raw else f"{raw}:443", True)


def run_audio_burst_cycle(
    *,
    gateway_url: str,
    phones: list[tuple[str, float, float, str]],
    frame_count: int,
) -> None:
    _ensure_pb_stubs()
    target, use_tls = _parse_gateway_target(gateway_url)
    drone_idx = random.randrange(len(phones))

    # Prefer real-WAV streaming if the sim VM's startup script has
    # cloned the audio fixtures; fall back to synthesis otherwise.
    real_pick = _pick_real_wav()
    if real_pick is not None:
        wav_label, wav_path = real_pick
        drone_profile = None
        drone_wav: Path | None = wav_path
        source_desc = f"real:{wav_label}/{wav_path.name}"
    else:
        drone_profile = _pick_drone_profile()
        drone_wav = None
        source_desc = f"synth:{drone_profile[0]}"

    print(
        f"audio-burst: gateway={target} tls={use_tls} frames={frame_count} "
        f"drone_sensor={phones[drone_idx][0]} source={source_desc}",
        flush=True,
    )

    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(phones)) as pool:
        for i, (did, lat, lon, site) in enumerate(phones):
            futures.append(
                pool.submit(
                    _stream_audio_burst_for_phone,
                    gateway_target=target,
                    use_tls=use_tls,
                    device_id=did,
                    site_label=site,
                    lat=lat,
                    lon=lon,
                    frame_count=frame_count,
                    is_drone=(i == drone_idx),
                    drone_profile=drone_profile if i == drone_idx else None,
                    drone_wav=drone_wav if i == drone_idx else None,
                )
            )
        for fut in concurrent.futures.as_completed(futures):
            did, ok, detail = fut.result()
            status = "OK" if ok else "FAIL"
            print(f"  audio-burst {status} {did}: {detail}", flush=True)


def main() -> int:
    args = parse_args()
    if not args.project:
        print(
            "Missing --project or GOOGLE_CLOUD_PROJECT.",
            file=sys.stderr,
        )
        return 2
    if (
        not args.register_only
        and not args.admin_url
        and not args.redis_host
        and not args.audio_burst
    ):
        print(
            "Missing --admin-url, --redis-host, --audio-burst, or --register-only.",
            file=sys.stderr,
        )
        return 2

    if args.audio_burst and not args.gateway_url:
        print(
            "--audio-burst requires --gateway-url (or $DRONE_SENSOR_GATEWAY_URL).",
            file=sys.stderr,
        )
        return 2

    phones = phone_fixtures(args)

    # If --audio-burst is the only mode specified (no heartbeat path),
    # run a self-contained audio-burst loop and return.
    if args.audio_burst and not args.admin_url and not args.redis_host and not args.register_only:
        stop = False

        def _stop(_signum: int, _frame: object) -> None:
            nonlocal stop
            stop = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        while not stop:
            run_audio_burst_cycle(
                gateway_url=args.gateway_url,
                phones=phones,
                frame_count=args.audio_burst_seconds,
            )
            if args.once:
                break
            for _ in range(args.interval_seconds):
                if stop:
                    break
                time.sleep(1)
        return 0

    if args.admin_url:
        stop = False

        def _stop(_signum: int, _frame: object) -> None:
            nonlocal stop
            stop = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        tick = 0
        while not stop:
            tick += 1
            ts = now_ms()
            post_to_admin_url(
                admin_url=args.admin_url,
                token=args.simulator_token,
                phones=phones,
                timestamp_ms=ts,
                ttl_seconds=args.redis_ttl_seconds,
                tick=tick,
            )
            print(
                f"posted {len(phones)} fake phones to {args.admin_url} at {ts}; "
                f"next refresh in {args.interval_seconds}s",
                flush=True,
            )
            if args.audio_burst:
                run_audio_burst_cycle(
                    gateway_url=args.gateway_url,
                    phones=phones,
                    frame_count=args.audio_burst_seconds,
                )
            if args.once:
                break
            for _ in range(args.interval_seconds):
                if stop:
                    break
                time.sleep(1)
        return 0

    from google.cloud import firestore

    db = firestore.Client(project=args.project, database=args.firestore_database)

    if args.register_only:
        ts = now_ms()
        public_keys = ensure_keypairs(phones, args.key_dir) if args.with_keypairs else None
        upsert_firestore_devices(
            db,
            collection=args.collection,
            geo_point=firestore.GeoPoint,
            phones=phones,
            timestamp_ms=ts,
            public_keys=public_keys,
        )
        key_text = f" with keypairs in {args.key_dir}" if args.with_keypairs else ""
        print(f"registered {len(phones)} fake phones{key_text} at {ts}", flush=True)
        return 0

    import redis

    redis_client = redis.Redis(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    redis_client.ping()

    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    tick = 0
    while not stop:
        tick += 1
        ts = now_ms()
        upsert_firestore_devices(
            db,
            collection=args.collection,
            geo_point=firestore.GeoPoint,
            phones=phones,
            timestamp_ms=ts,
            public_keys=None,
        )
        refresh_redis(
            redis_client,
            phones=phones,
            timestamp_ms=ts,
            ttl_seconds=args.redis_ttl_seconds,
            tick=tick,
        )
        print(
            f"refreshed {len(phones)} fake phones at {ts}; "
            f"next refresh in {args.interval_seconds}s",
            flush=True,
        )
        if args.audio_burst:
            run_audio_burst_cycle(
                gateway_url=args.gateway_url,
                phones=phones,
                frame_count=args.audio_burst_seconds,
            )
        if args.once:
            break
        for _ in range(args.interval_seconds):
            if stop:
                break
            time.sleep(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
