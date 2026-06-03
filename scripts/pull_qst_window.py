#!/usr/bin/env python3
"""Pull a single contiguous window of audio from the QST API.

Single-purpose tool for the 500-phone load test: grab one hour of real
Argos sensor audio around a known drone event, resample to 16 kHz, and
write a WAV that the replay-fleet simulator can loop as its source.

Distinct from yamnet-drone-detector's CSV-driven detection puller —
this one takes a sensor + start/end on the command line and pulls one
window. Auth + source resolution mirror the yamnet implementation; do
not edit the yamnet repo to keep them in sync, just rerun this if
auth/host details change.

Usage:
    .venv/bin/python scripts/pull_qst_window.py \\
        --sensor SH008 \\
        --start 2026-04-09T12:40:00Z \\
        --end   2026-04-09T13:40:00Z \\
        --out   /tmp/argos_sh008_2026-04-09_T1240Z_1h_16k.wav

Pulls in 5-minute chunks (so the server doesn't time out on a single
3600 s request) and concatenates the PCM bytes before writing.
Resamples to --target-sr (default 16000, matches phone capture);
pass --target-sr 0 to keep the native 8 kHz from the SH sensors.

Reads QST credentials from --env (default ./.env in this repo,
falling back to ../yamnet-drone-detector/.env so a one-shot pull
works without copying secrets across repos).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHANNEL = "Scell"
CHUNK_SECONDS_DEFAULT = 300
REQUEST_TIMEOUT = 120


def load_env(explicit: Path | None) -> dict:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    here = Path(__file__).resolve().parent.parent
    candidates += [
        here / ".env",
        here.parent / "yamnet-drone-detector" / ".env",
    ]
    for p in candidates:
        if p.exists():
            env = {}
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            required = ["QST_KC_URL", "QST_REALM", "QST_CLIENT_ID", "QST_CLIENT_SECRET", "QST_API_BASE"]
            missing = [k for k in required if not env.get(k)]
            if missing:
                sys.exit(f"Missing {missing} in {p}.")
            print(f"[env] loaded {p}", file=sys.stderr)
            return env
    sys.exit(f"No .env found in any of: {candidates}")


def get_token(env: dict) -> str:
    url = f"{env['QST_KC_URL']}/realms/{env['QST_REALM']}/protocol/openid-connect/token"
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": env["QST_CLIENT_ID"],
            "client_secret": env["QST_CLIENT_SECRET"],
        }
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        return json.load(r)["access_token"]


def resolve_source_id(env: dict, token: str) -> str:
    url = f"{env['QST_API_BASE']}/api/inventory/sources"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        sources = json.load(r)
    for s in sources:
        if s.get("source_type") == "bigquery":
            return s["id"]
    sys.exit(f"No BigQuery source found among: {[s.get('key') for s in sources]}")


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(s: str) -> datetime:
    # Accept "...Z" or with explicit +00:00.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_chunk(env: dict, token: str, source_id: str, sensor: str,
                start: datetime, end: datetime) -> tuple[bytes, int]:
    q = urllib.parse.urlencode(
        {
            "station": sensor,
            "channel": CHANNEL,
            "source_id": source_id,
            "start_time": iso_z(start),
            "end_time": iso_z(end),
        }
    )
    url = f"{env['QST_API_BASE']}/api/stream/audio?{q}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        body = r.read()
        sr = int(r.headers.get("x-sample-rate", "8000"))
    return body, sr


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull one QST audio window.")
    ap.add_argument("--sensor", required=True, help="e.g. SH008")
    ap.add_argument("--start", required=True, help="ISO-8601 UTC, e.g. 2026-04-09T12:40:00Z")
    ap.add_argument("--end", required=True, help="ISO-8601 UTC")
    ap.add_argument("--out", required=True, type=Path, help="Output WAV path")
    ap.add_argument("--chunk-seconds", type=int, default=CHUNK_SECONDS_DEFAULT,
                    help="Pull in chunks of this many seconds (default 300).")
    ap.add_argument("--target-sr", type=int, default=16000,
                    help="Resample to this rate (0 keeps native 8 kHz).")
    ap.add_argument("--env", type=Path, default=None, help="Override .env path.")
    args = ap.parse_args()

    start = parse_iso(args.start)
    end = parse_iso(args.end)
    if end <= start:
        sys.exit("--end must be after --start")
    total_s = (end - start).total_seconds()
    if args.target_sr and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found but --target-sr set. Install ffmpeg or pass --target-sr 0.")

    env = load_env(args.env)
    print(f"[auth] requesting token...", file=sys.stderr)
    token = get_token(env)
    source_id = resolve_source_id(env, token)
    print(f"[src] bq source_id={source_id}", file=sys.stderr)

    pcm_parts: list[bytes] = []
    native_sr: int | None = None
    chunks = max(1, int((total_s + args.chunk_seconds - 1) // args.chunk_seconds))
    for i in range(chunks):
        c_start = start + timedelta(seconds=i * args.chunk_seconds)
        c_end = min(end, c_start + timedelta(seconds=args.chunk_seconds))
        print(f"[fetch] {i + 1}/{chunks} {args.sensor} {iso_z(c_start)} → {iso_z(c_end)}",
              file=sys.stderr)
        body, sr = fetch_chunk(env, token, source_id, args.sensor, c_start, c_end)
        if native_sr is None:
            native_sr = sr
        elif sr != native_sr:
            sys.exit(f"Sample-rate drift mid-pull: {sr} vs {native_sr}")
        pcm_parts.append(body)
    pcm = b"".join(pcm_parts)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    duration_s = (len(pcm) // 2) / native_sr if native_sr else 0
    print(f"[fetch] done; native_sr={native_sr} bytes={len(pcm)} duration={duration_s:.1f}s",
          file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(native_sr or 8000)
        w.writeframes(pcm)
    print(f"[write] {args.out} ({native_sr} Hz, {duration_s:.1f}s)", file=sys.stderr)

    if args.target_sr and native_sr != args.target_sr:
        print(f"[resample] {native_sr} → {args.target_sr} Hz (ffmpeg)", file=sys.stderr)
        tmp = args.out.with_suffix(args.out.suffix + ".rs.tmp")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(args.out),
             "-ar", str(args.target_sr), "-ac", "1", "-c:a", "pcm_s16le",
             "-f", "wav", str(tmp)],
            check=True,
        )
        tmp.replace(args.out)
        print(f"[resample] done", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
