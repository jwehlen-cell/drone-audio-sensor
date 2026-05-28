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

DEFAULT_PHONE_FIXTURES = (
    ("DRONE-SENSOR-001", 26.6755, -80.0380, "Southern boundary intersection"),
    ("DRONE-SENSOR-002", 26.6762, -80.0365, "Approaching Beach Club southern edge"),
    ("DRONE-SENSOR-003", 26.6771, -80.0360, "East of main house / Tunnel link"),
    ("DRONE-SENSOR-004", 26.6783, -80.0358, "Northern property line"),
    ("DRONE-SENSOR-005", 26.6792, -80.0360, "Transitioning to northern neighbor"),
    ("DRONE-SENSOR-006", 26.6805, -80.0367, "Roadway curving sharply west"),
    ("DRONE-SENSOR-007", 26.6815, -80.0378, "Mid-curve baseline"),
    ("DRONE-SENSOR-008", 26.6823, -80.0388, "Northern approach buffer"),
    ("DRONE-SENSOR-009", 26.6828, -80.0395, "Outer northern perimeter"),
    ("DRONE-SENSOR-010", 26.677639, -80.039558, "Array termination point"),
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


def _make_drone_buzz_pcm16(seconds: int) -> bytes:
    import numpy as np
    sr = AUDIO_BURST_SAMPLE_RATE
    t = np.linspace(0, seconds, sr * seconds, endpoint=False)
    sig = (
        0.30 * np.sin(2 * np.pi * 180.0 * t)
        + 0.22 * np.sin(2 * np.pi * 360.0 * t)
        + 0.12 * np.sin(2 * np.pi * 540.0 * t)
        + 0.05 * np.random.randn(t.size)
    )
    sig = np.clip(sig * 0.7, -1.0, 1.0)
    return (sig * 32767).astype(np.int16).tobytes()


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
) -> tuple[str, bool, str]:
    """Open a single gRPC bidi stream, push a handshake + frame_count audio
    frames, then close. Returns (device_id, ok, detail)."""
    import grpc
    import drone_audio_pb2 as pb
    import drone_audio_pb2_grpc as pb_grpc

    sr = AUDIO_BURST_SAMPLE_RATE
    frame_seconds = AUDIO_BURST_FRAME_SECONDS

    if is_drone:
        pcm_chunks = [_make_drone_buzz_pcm16(frame_seconds) for _ in range(frame_count)]
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
                yield pb.ClientStreamMessage(
                    audio_frame=pb.AudioFrame(
                        device_id=device_id,
                        capture_timestamp_ms=now_ms(),
                        sequence_number=i + 1,
                        sample_rate_hz=sr,
                        pcm16_mono=pcm,
                    )
                )
                # Mild pacing so the gateway's per-stream state has a chance
                # to flush each frame to Redis before the next arrives.
                time.sleep(0.05)

        # Drain server-side responses; we don't act on them but we need to
        # consume the iterator for the stream to close cleanly.
        for _resp in stub.StreamAudio(request_iter(), timeout=30):
            pass
        return (device_id, True, "drone-buzz" if is_drone else "noise")
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

    print(
        f"audio-burst: gateway={target} tls={use_tls} frames={frame_count} "
        f"drone_sensor={phones[drone_idx][0]}",
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
