"""Egress-test status dashboard.

Cloud Run service in drone-audio-sensor. Reads the `test_runs`
Firestore collection that the audio_receiver (and any future
receivers) write to, renders a single HTML page with one row per
test run. Auto-refreshes every 10 s.

Two surfaces:
  GET /            -> HTML dashboard
  GET /api/runs    -> JSON dump of the same data (for scripting)

Each test run row shows: tag, receiver type, status (running |
complete), started, last update, duration, plus the per-test
counters the receiver writes (handshakes, frames, wire bytes,
audio payload, PCM equivalent, FLAC compression ratio,
max-single-frame, errors).

Stale 'running' runs (no updates in > STALE_SECONDS) are flipped
to 'complete' by the receiver's own background sweep; the dashboard
also marks them visually in case the sweep is late.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.cloud import firestore

COLLECTION = "test_runs"
STALE_SECONDS = 300


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if f < 1024:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{f:.2f} EiB"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _utc_iso(ts) -> str:
    if ts is None:
        return ""
    try:
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:  # noqa: BLE001
        return str(ts)


def _row_to_view(doc_id: str, d: dict) -> dict:
    started_at = d.get("started_at")
    last_updated_at = d.get("last_updated_at")
    ended_at = d.get("ended_at")
    status = d.get("status", "unknown")

    now = datetime.now(timezone.utc)
    started_dt = started_at if hasattr(started_at, "astimezone") else None
    last_dt = last_updated_at if hasattr(last_updated_at, "astimezone") else None
    ended_dt = ended_at if hasattr(ended_at, "astimezone") else None

    if status == "running" and last_dt is not None:
        # Server-side stale check, in case the receiver's sweep is
        # behind. Lets the dashboard show "complete (auto)" without
        # waiting for the next sweep cycle.
        age_s = (now - last_dt).total_seconds()
        if age_s > STALE_SECONDS:
            status = "complete*"

    duration_s = 0.0
    if started_dt is not None:
        end = ended_dt or (last_dt if status.startswith("complete") else now)
        duration_s = max(0.0, (end - started_dt).total_seconds())

    wire = int(d.get("wire_bytes", 0))
    audio = int(d.get("audio_payload_bytes", 0))
    pcm_eq = int(d.get("pcm_equivalent_bytes", 0))
    compression = (1 - audio / pcm_eq) * 100 if pcm_eq > 0 else 0.0
    return {
        "test_run_tag": doc_id,
        "receiver_type": d.get("receiver_type", ""),
        "status": status,
        "started": _utc_iso(started_dt),
        "last_updated": _utc_iso(last_dt),
        "ended": _utc_iso(ended_dt),
        "duration": _fmt_duration(duration_s),
        "duration_s": duration_s,
        "handshakes": int(d.get("handshakes_received", 0)),
        "frames": int(d.get("frames_received", 0)),
        "wire_bytes": wire,
        "wire_bytes_fmt": _fmt_bytes(wire),
        "audio_payload_bytes": audio,
        "audio_payload_fmt": _fmt_bytes(audio),
        "pcm_equivalent_bytes": pcm_eq,
        "pcm_equivalent_fmt": _fmt_bytes(pcm_eq),
        "compression_pct": compression,
        "frames_flac": int(d.get("frames_flac", 0)),
        "frames_wav": int(d.get("frames_wav", 0)),
        "frames_pcm16": int(d.get("frames_pcm16", 0)),
        "frames_unknown_codec": int(d.get("frames_unknown_codec", 0)),
        "max_single_frame_bytes": int(d.get("max_single_frame_bytes", 0)),
        "max_single_frame_fmt": _fmt_bytes(int(d.get("max_single_frame_bytes", 0))),
        "stream_errors": int(d.get("stream_errors", 0)),
    }


def _load_runs(db) -> list[dict]:
    docs = list(
        db.collection(COLLECTION)
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(50)
        .stream()
    )
    return [_row_to_view(s.id, s.to_dict() or {}) for s in docs]


HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Egress test status</title>
<meta http-equiv="refresh" content="10">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif;
       background: #0f1419; color: #e6edf3; margin: 0; padding: 24px; }}
h1 {{ font-size: 18px; margin: 0 0 6px; }}
.sub {{ color: #8b949e; font-size: 12px; margin-bottom: 18px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th, td {{ padding: 6px 10px; text-align: left;
          border-bottom: 1px solid #21262d; vertical-align: top; }}
th {{ position: sticky; top: 0; background: #161b22; color: #8b949e;
      font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
      font-size: 11px; }}
tr.running {{ background: #0e2618; }}
tr.complete {{ background: transparent; }}
tr.complete-auto {{ background: #1a1a0e; }}
.status {{ display: inline-block; padding: 2px 8px; border-radius: 3px;
           font-weight: 600; font-size: 11px; }}
.status.running {{ background: #1f6f3a; color: #d3f7d3; }}
.status.complete {{ background: #21262d; color: #8b949e; }}
.status.complete-auto {{ background: #574d2a; color: #ffd966; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.tag {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-weight: 600; color: #58a6ff; }}
.muted {{ color: #8b949e; }}
.empty {{ text-align: center; padding: 40px; color: #8b949e; }}
</style>
</head>
<body>
<h1>Egress test status</h1>
<div class="sub">drone-audio-sensor &middot; {n} run(s) &middot; rendered {rendered} &middot;
  auto-refresh 10 s</div>
{body}
</body>
</html>
"""

TABLE_HEADER = """
<table>
<thead>
<tr>
  <th>Test run</th>
  <th>Type</th>
  <th>Status</th>
  <th>Started</th>
  <th>Duration</th>
  <th class="num">Handshakes</th>
  <th class="num">Frames</th>
  <th class="num">Wire bytes</th>
  <th class="num">Audio payload</th>
  <th class="num">PCM equivalent</th>
  <th class="num">Compression</th>
  <th class="num">Max single</th>
  <th class="num">Errors</th>
  <th>Codecs</th>
  <th>Last update</th>
</tr>
</thead>
<tbody>
"""


def _row_html(r: dict) -> str:
    status = r["status"]
    css_class = "running" if status == "running" else (
        "complete-auto" if status == "complete*" else "complete"
    )
    status_class = css_class
    codecs = []
    for c, k in (("flac","frames_flac"),("wav","frames_wav"),
                 ("pcm16","frames_pcm16"),("unk","frames_unknown_codec")):
        if r[k] > 0:
            codecs.append(f"{c}:{r[k]}")
    codecs_str = " ".join(codecs) or "—"
    return (
        f'<tr class="{css_class}">'
        f'<td class="tag">{r["test_run_tag"]}</td>'
        f'<td class="muted">{r["receiver_type"]}</td>'
        f'<td><span class="status {status_class}">{status}</span></td>'
        f'<td class="muted">{r["started"]}</td>'
        f'<td>{r["duration"]}</td>'
        f'<td class="num">{r["handshakes"]:,}</td>'
        f'<td class="num">{r["frames"]:,}</td>'
        f'<td class="num">{r["wire_bytes_fmt"]}</td>'
        f'<td class="num">{r["audio_payload_fmt"]}</td>'
        f'<td class="num">{r["pcm_equivalent_fmt"]}</td>'
        f'<td class="num">{r["compression_pct"]:.1f}%</td>'
        f'<td class="num">{r["max_single_frame_fmt"]}</td>'
        f'<td class="num">{r["stream_errors"]}</td>'
        f'<td class="muted">{codecs_str}</td>'
        f'<td class="muted">{r["last_updated"]}</td>'
        f'</tr>'
    )


def _render(rows: list[dict]) -> bytes:
    if not rows:
        body = '<div class="empty">No test runs yet. Start a test and reload.</div>'
    else:
        body = TABLE_HEADER + "".join(_row_html(r) for r in rows) + "</tbody></table>"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return HTML_PAGE.format(n=len(rows), rendered=now, body=body).encode()


class _Handler(BaseHTTPRequestHandler):
    db: firestore.Client = None  # set by main()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            try:
                rows = _load_runs(self.db)
            except Exception as e:  # noqa: BLE001
                self._send(500, f"firestore error: {e}".encode(), "text/plain")
                return
            self._send(200, _render(rows), "text/html; charset=utf-8")
            return
        if self.path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
            return
        if self.path == "/api/runs":
            try:
                rows = _load_runs(self.db)
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
                return
            self._send(200, json.dumps({"runs": rows}, default=str).encode(),
                       "application/json")
            return
        self._send(404, b"not found\n", "text/plain")

    def log_message(self, fmt, *args):  # noqa: ANN001
        return


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    _Handler.db = firestore.Client()
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"test_status listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
