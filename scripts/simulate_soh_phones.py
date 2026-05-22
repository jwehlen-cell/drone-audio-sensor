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
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


DEFAULT_BASE_LAT = 38.8977
DEFAULT_BASE_LON = -77.0365
DEFAULT_INTERVAL_SECONDS = 180
DEFAULT_REDIS_TTL_SECONDS = 300

DEFAULT_PHONE_FIXTURES = (
    ("DRONE-SENSOR-001", 26.670000, -80.031900, "Simulated Beachline South"),
    ("DRONE-SENSOR-002", 26.672000, -80.031700, "Simulated Beachline"),
    ("DRONE-SENSOR-003", 26.674000, -80.031500, "Simulated Beachline"),
    ("DRONE-SENSOR-004", 26.676000, -80.031300, "Simulated Beachline"),
    ("DRONE-SENSOR-005", 26.678000, -80.031100, "Simulated Beachline"),
    ("DRONE-SENSOR-006", 26.680000, -80.030900, "Simulated Beachline"),
    ("DRONE-SENSOR-007", 26.682000, -80.030700, "Simulated Beachline"),
    ("DRONE-SENSOR-008", 26.684000, -80.030500, "Simulated Beachline"),
    ("DRONE-SENSOR-009", 26.686000, -80.030300, "Simulated Beachline"),
    ("DRONE-SENSOR-010", 26.688000, -80.030100, "Simulated Beachline North"),
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
) -> None:
    batch = db.batch()
    for did, lat, lon, site in phones:
        doc = db.collection(collection).document(did)
        batch.set(
            doc,
            {
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
            },
            merge=True,
        )
    batch.commit()


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


def main() -> int:
    args = parse_args()
    if not args.project:
        print(
            "Missing --project or GOOGLE_CLOUD_PROJECT.",
            file=sys.stderr,
        )
        return 2
    if not args.register_only and not args.admin_url and not args.redis_host:
        print(
            "Missing --admin-url or --redis-host. Pass --register-only to write Firestore docs only.",
            file=sys.stderr,
        )
        return 2

    phones = phone_fixtures(args)

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
        upsert_firestore_devices(
            db,
            collection=args.collection,
            geo_point=firestore.GeoPoint,
            phones=phones,
            timestamp_ms=ts,
        )
        print(f"registered {len(phones)} fake phones at {ts}", flush=True)
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
        if args.once:
            break
        for _ in range(args.interval_seconds):
            if stop:
                break
            time.sleep(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
