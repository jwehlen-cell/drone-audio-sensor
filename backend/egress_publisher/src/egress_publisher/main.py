"""Egress-cost measurement publisher.

Subscribes to the production detections Pub/Sub topic, batches N
detections in memory, serializes the batch as a dense protobuf
(egress_batch.proto), gzip-compresses, and POSTs to a configured
external receiver. The point is to exit the most-efficient envelope
possible so the egress-out byte count is a defensible lower bound
on real-world detection delivery cost.

Wire pipeline:
    Pub/Sub JSON (~1.5 KB)
        -> Detection protobuf (~150-300 B)
        -> EgressBatch protobuf (N detections)
        -> gzip (typically 50-90% reduction on batched protobuf)
        -> HTTP/2 POST with Content-Encoding: gzip
        -> persistent httpx client (TCP+TLS handshake amortized)

Cloud Run also exposes a /healthz endpoint on $PORT so the platform
health checks don't crash-loop the container at deploy time.

Env knobs (all optional, defaults shown):

  EGRESS_DETECTIONS_SUBSCRIPTION  (required)
      e.g. projects/argosuat/subscriptions/aftac-argosuat-detections-egress
  EGRESS_TARGET_URL               (required)
      e.g. https://<ngrok>.ngrok.io/egress
  EGRESS_BATCH_SIZE        100      flush when this many detections
                                    are buffered
  EGRESS_BATCH_TIMEOUT_S   5.0      flush after this long even if
                                    not full
  EGRESS_COMPRESSION       gzip     gzip|none
  EGRESS_AUTH_HEADER       ""       optional Authorization header
  EGRESS_HEALTH_PORT       8080     PORT from Cloud Run
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import structlog
from google.cloud import pubsub_v1

import egress_batch_pb2 as pb

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    subscription: str
    target_url: str
    batch_size: int = 100
    batch_timeout_s: float = 5.0
    compression: str = "gzip"
    auth_header: str = ""
    health_port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        sub = os.environ.get("EGRESS_DETECTIONS_SUBSCRIPTION", "").strip()
        url = os.environ.get("EGRESS_TARGET_URL", "").strip()
        if not sub:
            raise SystemExit("EGRESS_DETECTIONS_SUBSCRIPTION env var is required")
        if not url:
            raise SystemExit("EGRESS_TARGET_URL env var is required")
        return cls(
            subscription=sub,
            target_url=url,
            batch_size=int(os.environ.get("EGRESS_BATCH_SIZE", "100")),
            batch_timeout_s=float(os.environ.get("EGRESS_BATCH_TIMEOUT_S", "5.0")),
            compression=os.environ.get("EGRESS_COMPRESSION", "gzip").lower(),
            auth_header=os.environ.get("EGRESS_AUTH_HEADER", ""),
            health_port=int(os.environ.get("PORT")
                            or os.environ.get("EGRESS_HEALTH_PORT", "8080")),
        )


# ---------------------------------------------------------------------------
# Pub/Sub → protobuf detection
# ---------------------------------------------------------------------------

def to_proto(body: dict) -> pb.Detection:
    """Lift the JSON detection from the prod Pub/Sub topic into the
    dense protobuf wire format. Tolerates missing nested fields by
    leaving them at proto default (0 / empty string)."""
    d = pb.Detection()
    d.device_id = body.get("device_id") or ""
    d.detection_id = body.get("detection_id") or ""
    d.first_frame_ts_ms = int(body.get("first_frame_timestamp_ms") or 0)
    d.last_frame_ts_ms = int(body.get("last_frame_timestamp_ms") or 0)
    d.peak_score = float(body.get("peak_score") or 0.0)
    d.average_score = float(body.get("average_score") or 0.0)
    d.threshold = float(body.get("threshold") or 0.0)
    d.frames_over_threshold = int(body.get("frames_over_threshold") or 0)
    d.window_frames = int(body.get("window_frames") or 0)
    cat = body.get("category") or {}
    d.category = cat.get("token") if isinstance(cat, dict) else (cat or "")
    sub = body.get("subtype") or {}
    if isinstance(sub, dict):
        d.subtype_label = sub.get("label") or ""
        d.subtype_confidence = float(sub.get("confidence") or 0.0)
    d.site = body.get("site") or ""
    loc = body.get("device_location") or {}
    if isinstance(loc, dict):
        d.location_lat = float(loc.get("latitude") or 0.0)
        d.location_lon = float(loc.get("longitude") or 0.0)
    d.published_at_ms = int(body.get("published_at_ms") or 0)
    model = body.get("model") or {}
    if isinstance(model, dict):
        d.model_name = model.get("name") or ""
        d.model_version = model.get("version") or ""
    return d


# ---------------------------------------------------------------------------
# Forwarder (batcher + HTTP push)
# ---------------------------------------------------------------------------

class Forwarder:
    """Buffer up to batch_size detections; flush on size or timeout.
    Tracks bytes sent + compression ratio for the test report."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._buf: list[tuple[pb.Detection, Callable[[], None]]] = []
        self._lock = asyncio.Lock()
        # httpx async client gets HTTP/2 + connection pooling for
        # free; verify=True is the default but kept explicit so the
        # ngrok TLS chain is checked.
        self._client = httpx.AsyncClient(
            http2=True, verify=True, timeout=30.0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self.stats = {
            "batches_sent": 0,
            "wire_bytes": 0,
            "uncompressed_bytes": 0,
            "json_input_bytes": 0,
            "detections_sent": 0,
            "errors": 0,
            "started_at": time.time(),
        }
        self._flush_event = asyncio.Event()
        self._stopped = False

    async def enqueue(
        self, det: pb.Detection, ack: Callable[[], None], raw_size: int
    ) -> None:
        async with self._lock:
            self._buf.append((det, ack))
            self.stats["json_input_bytes"] += raw_size
            if len(self._buf) >= self._s.batch_size:
                self._flush_event.set()

    async def run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(), timeout=self._s.batch_timeout_s
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            await self._flush_once()

    async def _flush_once(self) -> None:
        async with self._lock:
            if not self._buf:
                return
            items = self._buf
            self._buf = []

        batch = pb.EgressBatch()
        for det, _ack in items:
            batch.detections.append(det)
        raw = batch.SerializeToString()

        if self._s.compression == "gzip":
            payload = gzip.compress(raw, compresslevel=6)
            content_encoding = "gzip"
        else:
            payload = raw
            content_encoding = ""

        headers = {"Content-Type": "application/x-protobuf"}
        if content_encoding:
            headers["Content-Encoding"] = content_encoding
        if self._s.auth_header:
            headers["Authorization"] = self._s.auth_header

        try:
            resp = await self._client.post(
                self._s.target_url, content=payload, headers=headers
            )
            ok = 200 <= resp.status_code < 300
        except Exception as e:  # noqa: BLE001
            log.warning("egress_post_failed", error=str(e), n=len(items))
            ok = False

        if ok:
            self.stats["batches_sent"] += 1
            self.stats["wire_bytes"] += len(payload)
            self.stats["uncompressed_bytes"] += len(raw)
            self.stats["detections_sent"] += len(items)
            for _det, ack in items:
                try:
                    ack()
                except Exception:  # noqa: BLE001
                    pass
        else:
            self.stats["errors"] += 1
            # Re-queue so Pub/Sub redelivers via nack timeout. Don't
            # ack the message.

    async def stop(self) -> None:
        self._stopped = True
        self._flush_event.set()
        # One last flush.
        await self._flush_once()
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Pub/Sub subscriber bridge
# ---------------------------------------------------------------------------

class Subscriber:
    def __init__(self, settings: Settings, forwarder: Forwarder,
                 loop: asyncio.AbstractEventLoop) -> None:
        self._s = settings
        self._fwd = forwarder
        self._loop = loop
        self._client = pubsub_v1.SubscriberClient()
        self._future = None

    def start(self) -> None:
        flow = pubsub_v1.types.FlowControl(
            max_messages=256, max_bytes=10 * 1024 * 1024
        )
        self._future = self._client.subscribe(
            self._s.subscription, callback=self._on_message,
            flow_control=flow,
        )
        log.info("subscribed", subscription=self._s.subscription)

    def stop(self) -> None:
        if self._future is not None:
            self._future.cancel()
            try:
                self._future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def _on_message(self, message) -> None:  # noqa: ANN001
        raw = message.data
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            message.ack()
            return
        det = to_proto(body)
        # Cross-thread enqueue: Pub/Sub callback fires on a
        # threadpool, the forwarder runs on the asyncio loop.
        asyncio.run_coroutine_threadsafe(
            self._fwd.enqueue(det, message.ack, len(raw)), self._loop
        )


# ---------------------------------------------------------------------------
# Stats / health HTTP
# ---------------------------------------------------------------------------

def _start_health_server(port: int, forwarder: Forwarder) -> threading.Thread:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            if self.path == "/stats":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(forwarder.stats).encode())
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):  # noqa: ANN001
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _amain(settings: Settings) -> None:
    loop = asyncio.get_running_loop()
    forwarder = Forwarder(settings)
    subscriber = Subscriber(settings, forwarder, loop)

    _start_health_server(settings.health_port, forwarder)
    subscriber.start()

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(forwarder.run())

    async def _report() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
                return
            except asyncio.TimeoutError:
                pass
            log.info("egress_stats", **forwarder.stats)

    reporter = asyncio.create_task(_report())
    await stop.wait()
    reporter.cancel()
    try:
        await reporter
    except asyncio.CancelledError:
        pass

    subscriber.stop()
    await forwarder.stop()
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass

    log.info("egress_final", **forwarder.stats)


def main() -> int:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    settings = Settings.from_env()
    log.info(
        "egress_publisher_starting",
        target=settings.target_url,
        batch_size=settings.batch_size,
        batch_timeout_s=settings.batch_timeout_s,
        compression=settings.compression,
    )
    asyncio.run(_amain(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
