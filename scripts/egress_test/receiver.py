#!/usr/bin/env python3
"""Local egress receiver. Accepts POSTs from the cloud egress
publisher, decompresses gzip bodies to measure post-decompress size,
and drops the data on the floor.

The point is to measure how many bytes GCP egressed -- not to do
anything with the detections. So this is deliberately a single-file
Python stdlib server with no dependencies.

Stats reported:
  * total wire bytes received (the GCP egress cost basis)
  * total uncompressed bytes (what the consumer would have seen)
  * compression ratio
  * request count + max single request
  * sustained byte rate

Run on the laptop:
    python3 receiver.py --port 8080

Expose to the public internet (so the Cloud Run publisher can hit it):
    ngrok http 8080
And set EGRESS_TARGET_URL in the cloud publisher to the ngrok URL.

On SIGINT, prints a final summary then exits.
"""
from __future__ import annotations

import argparse
import gzip
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATS = {
    "wire_bytes": 0,
    "uncompressed_bytes": 0,
    "requests": 0,
    "max_request_size": 0,
    "started_at": time.time(),
}
LOCK = threading.Lock()


def _fmt_bytes(n: float) -> str:
    # Auto-scale to the most readable unit. Always 2 decimal places
    # so eyeballing percentage changes between reports is easy.
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.2f} {units[i]}"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        encoding = (self.headers.get("Content-Encoding") or "").lower()
        uncompressed_size = length
        if encoding == "gzip":
            try:
                uncompressed_size = len(gzip.decompress(body))
            except Exception:  # noqa: BLE001
                # Malformed gzip; count wire bytes anyway.
                pass

        with LOCK:
            STATS["wire_bytes"] += length
            STATS["uncompressed_bytes"] += uncompressed_size
            STATS["requests"] += 1
            if length > STATS["max_request_size"]:
                STATS["max_request_size"] = length

        # 204 No Content -- "received, dropping, don't bother sending
        # me anything in the body".
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        # GET /healthz so ngrok / a curl can verify the server is up
        # without polluting the egress count.
        if self.path in ("/", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"egress receiver alive\n")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):  # noqa: A003, ANN001
        # Suppress the per-request access log; the periodic stats
        # report is plenty.
        return


def _report(label: str = "stats") -> None:
    elapsed = max(0.001, time.time() - STATS["started_at"])
    with LOCK:
        wire = STATS["wire_bytes"]
        unc = STATS["uncompressed_bytes"]
        reqs = STATS["requests"]
        peak = STATS["max_request_size"]
    print()
    print(f"=== egress receiver {label} ({elapsed:.1f}s elapsed) ===")
    print(f"  requests:              {reqs:>10,}")
    print(f"  wire bytes total:      {_fmt_bytes(wire):>14}  ({wire:,} bytes)")
    print(f"  uncompressed total:    {_fmt_bytes(unc):>14}  ({unc:,} bytes)")
    if reqs > 0:
        print(f"  avg per request:       {_fmt_bytes(wire / reqs)} wire / {_fmt_bytes(unc / reqs)} unc")
    if unc > 0:
        ratio = 1 - (wire / unc)
        print(f"  gzip compression:      {ratio * 100:.1f}%")
    print(f"  sustained wire rate:   {_fmt_bytes(wire / elapsed)}/s")
    print(f"  sustained unc rate:    {_fmt_bytes(unc / elapsed)}/s")
    print(f"  max single request:    {_fmt_bytes(peak)}")
    sys.stdout.flush()


def _stats_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        _report("rolling")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--report-interval", type=float, default=10.0,
                    help="Seconds between rolling stats reports (default 10).")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), _Handler)
    threading.Thread(target=_stats_loop, args=(args.report_interval,),
                     daemon=True).start()

    def _shutdown(signum, _frame):  # noqa: ANN001
        print(f"\nReceived signal {signum}; printing final report and exiting.")
        _report("FINAL")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"egress receiver listening on 0.0.0.0:{args.port}")
    print(f"rolling stats every {args.report_interval}s, Ctrl-C for final report")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
