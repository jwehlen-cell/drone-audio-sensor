"""Cross-project gRPC egress publisher.

Subscribes to the prod detections Pub/Sub topic in argosuat, packs N
detections into an EgressBatch protobuf, and pushes the batches over
one long-lived bidirectional gRPC stream to the egress_receiver in
drone-audio-sensor. Each batch is ack'd by the receiver, and only
then is the underlying Pub/Sub message ack'd -- so a stream death or
receiver outage gets the messages redelivered, not lost.

Wire pipeline now:
    Pub/Sub JSON (~1.5 KB)
        -> Detection protobuf (~150-300 B)
        -> EgressBatch (N detections, no JSON, no gzip headers)
        -> gRPC bidi stream (HTTP/2 + protobuf framing)
        -> persistent connection across whole publisher lifetime,
           one stream per process

Why gRPC instead of HTTP+gzip+POST:
  * persistent stream removes per-batch TCP+TLS handshake CPU
  * Cloud Run requests are billed per request; gRPC = 1 request per
    stream lifetime, HTTP = 1 request per batch
  * native protobuf encoding -- no Content-Type + Content-Encoding
    headers per batch, no chunked transfer framing overhead
  * matches the production phone-app shape (persistent streams)

Auth: cross-project. The publisher runs in argosuat under its own SA;
the receiver runs in drone-audio-sensor. We fetch a Google ID token
for the receiver's audience (the Cloud Run URL) and attach it via
gRPC metadata. Token rotation is handled by IDTokenCredentials.

Env knobs (all optional, defaults shown where given):

  EGRESS_DETECTIONS_SUBSCRIPTION  (required)
      e.g. projects/argosuat/subscriptions/aftac-argosuat-detections-egress
  EGRESS_RECEIVER_TARGET          (required, gRPC host:port)
      e.g. egress-receiver-xxx.us-west2.run.app:443
  EGRESS_RECEIVER_AUDIENCE        (defaults to "https://<host>")
      override only if your receiver is behind a non-default audience
  EGRESS_BATCH_SIZE        100      flush when this many detections
                                    are buffered
  EGRESS_BATCH_TIMEOUT_S   5.0      flush after this long even if
                                    not full
  EGRESS_HEALTH_PORT       8080     (PORT from Cloud Run)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

import grpc
import grpc.aio
import structlog
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import pubsub_v1
from google.oauth2 import id_token as id_token_lib

import egress_batch_pb2 as eb_pb  # type: ignore
import egress_service_pb2 as svc_pb  # type: ignore
import egress_service_pb2_grpc as svc_grpc  # type: ignore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    subscription: str
    receiver_target: str        # gRPC host:port, e.g. "egress-receiver-xxx.run.app:443"
    receiver_audience: str      # the audience the receiver expects in the ID token
    batch_size: int = 100
    batch_timeout_s: float = 5.0
    health_port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        sub = os.environ.get("EGRESS_DETECTIONS_SUBSCRIPTION", "").strip()
        target = os.environ.get("EGRESS_RECEIVER_TARGET", "").strip()
        if not sub:
            raise SystemExit("EGRESS_DETECTIONS_SUBSCRIPTION env var is required")
        if not target:
            raise SystemExit("EGRESS_RECEIVER_TARGET env var is required (host:port)")
        # Default audience = https URL with the host portion. Cloud
        # Run identity tokens expect the audience to match the
        # service's invocation URL.
        host = target.split(":", 1)[0]
        audience = os.environ.get(
            "EGRESS_RECEIVER_AUDIENCE", f"https://{host}",
        )
        return cls(
            subscription=sub,
            receiver_target=target,
            receiver_audience=audience,
            batch_size=int(os.environ.get("EGRESS_BATCH_SIZE", "100")),
            batch_timeout_s=float(os.environ.get("EGRESS_BATCH_TIMEOUT_S", "5.0")),
            health_port=int(
                os.environ.get("PORT") or os.environ.get("EGRESS_HEALTH_PORT", "8080"),
            ),
        )


# ---------------------------------------------------------------------------
# JSON detection -> protobuf Detection
# ---------------------------------------------------------------------------

def to_proto(body: dict) -> eb_pb.Detection:
    """Lift the JSON detection from the prod Pub/Sub topic into the
    dense protobuf wire format. Tolerates missing nested fields by
    leaving them at proto default (0 / empty string)."""
    d = eb_pb.Detection()
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
# Cross-project gRPC channel (Cloud Run service-to-service auth)
# ---------------------------------------------------------------------------

class _IdTokenRefresher:
    """Caches the receiver-audience ID token, refreshing every ~50 min
    (Google ID tokens last 1 h). Thread-safe."""

    def __init__(self, audience: str) -> None:
        self._audience = audience
        self._req = GoogleAuthRequest()
        self._token: str = ""
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if time.time() - self._fetched_at < 3000 and self._token:
                return self._token
            self._token = id_token_lib.fetch_id_token(self._req, self._audience)
            self._fetched_at = time.time()
            return self._token


@dataclass
class _PendingBatch:
    """Bookkeeping for a batch that's been written to the stream and
    is awaiting BatchAck. The acks list runs Pub/Sub acks once the
    receiver confirms the bytes landed."""
    seq: int
    acks: list[Callable[[], None]] = field(default_factory=list)
    proto_bytes: int = 0
    detection_count: int = 0


# ---------------------------------------------------------------------------
# Forwarder: gRPC bidi stream lifecycle
# ---------------------------------------------------------------------------

class GrpcForwarder:
    """One long-lived gRPC channel + bidi stream to the receiver.

    Buffers detections in memory. Flushes a batch as soon as either
    (a) the batch fills to batch_size or (b) batch_timeout_s elapses.
    A BatchAck from the receiver triggers Pub/Sub acks for the
    detections that made it into that batch -- so a Pub/Sub message
    only gets removed from the queue after the receiver has confirmed
    seeing its bytes.

    On stream error (Cloud Run scale-down, ID-token expiry, etc.) the
    inner _run_session loop returns and the outer reconnect loop opens
    a fresh stream after a brief backoff.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._id_tokens = _IdTokenRefresher(settings.receiver_audience)

        # Per-process detection buffer that the cadence/timer drains
        # into batches. Bounded so memory doesn't grow if the stream
        # is wedged for a long time.
        self._buf: list[tuple[eb_pb.Detection, Callable[[], None], int]] = []
        self._buf_lock = asyncio.Lock()
        self._flush_event = asyncio.Event()

        # Outbound queue of EgressBatch messages waiting for the gRPC
        # writer. Each item is (batch, _PendingBatch). The writer
        # yields the batch into the stream and stores the pending
        # entry so the reader can match it to the BatchAck.
        self._out_q: Optional[asyncio.Queue] = None

        self._batch_seq = 0
        self._stopped = False

        self.stats = {
            "batches_sent": 0,
            "batches_acked": 0,
            "wire_bytes": 0,
            "proto_bytes": 0,
            "json_input_bytes": 0,
            "detections_sent": 0,
            "errors": 0,
            "reconnects": 0,
            "started_at": time.time(),
        }

    async def enqueue(
        self, det: eb_pb.Detection, ack: Callable[[], None], raw_size: int,
    ) -> None:
        async with self._buf_lock:
            self._buf.append((det, ack, raw_size))
            self.stats["json_input_bytes"] += raw_size
            if len(self._buf) >= self._s.batch_size:
                self._flush_event.set()

    async def _drain_into_batches(self) -> None:
        """Reads from the detection buffer, packs EgressBatch messages
        of <= batch_size detections, and puts them on the outbound
        queue. Runs forever; the gRPC writer pulls from there."""
        assert self._out_q is not None
        while not self._stopped:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(), timeout=self._s.batch_timeout_s,
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()

            async with self._buf_lock:
                if not self._buf:
                    continue
                items = self._buf
                self._buf = []

            self._batch_seq += 1
            batch = eb_pb.EgressBatch()
            for det, _ack, _size in items:
                batch.detections.append(det)
            proto_bytes = batch.ByteSize()
            pending = _PendingBatch(
                seq=self._batch_seq,
                acks=[a for (_d, a, _s) in items],
                proto_bytes=proto_bytes,
                detection_count=len(items),
            )
            await self._out_q.put((batch, pending))

    async def _run_session(self) -> None:
        """Open one channel + stream, write batches, read acks until
        the stream errors. Caller (run()) handles reconnect."""
        token = self._id_tokens.token()
        creds = grpc.ssl_channel_credentials()
        call_credentials = grpc.access_token_call_credentials(token)
        composite = grpc.composite_channel_credentials(creds, call_credentials)
        channel = grpc.aio.secure_channel(
            self._s.receiver_target, composite,
            options=[
                ("grpc.keepalive_time_ms", 300_000),
                ("grpc.keepalive_timeout_ms", 20_000),
                ("grpc.keepalive_permit_without_calls", 1),
                ("grpc.max_send_message_length", 32 * 1024 * 1024),
            ],
        )
        stub = svc_grpc.EgressReceiverStub(channel)

        # Track batches awaiting ack, keyed by batch seq.
        pending: dict[int, _PendingBatch] = {}

        async def request_iter():
            while not self._stopped:
                item = await self._out_q.get()
                if item is None:
                    return
                batch, p = item
                pending[p.seq] = p
                yield batch
                self.stats["batches_sent"] += 1

        call = stub.StreamBatches(request_iter())
        try:
            async for ack in call:
                p = pending.pop(ack.batch_seq, None)
                if p is None:
                    continue
                self.stats["batches_acked"] += 1
                self.stats["wire_bytes"] += int(ack.server_received_bytes)
                self.stats["proto_bytes"] += p.proto_bytes
                self.stats["detections_sent"] += p.detection_count
                for ack_fn in p.acks:
                    try:
                        ack_fn()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            await channel.close()

        # Any pending batches whose acks never arrived get failed:
        # don't ack the Pub/Sub messages (they'll redeliver after the
        # subscription's ack deadline).
        for p in pending.values():
            self.stats["errors"] += 1

    async def run(self) -> None:
        self._out_q = asyncio.Queue(maxsize=64)
        drainer = asyncio.create_task(self._drain_into_batches())
        try:
            while not self._stopped:
                try:
                    await self._run_session()
                except grpc.aio.AioRpcError as e:
                    log.warning("stream_error", code=e.code().name, detail=e.details())
                    self.stats["errors"] += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("stream_unknown_error", error=str(e))
                    self.stats["errors"] += 1
                if self._stopped:
                    break
                self.stats["reconnects"] += 1
                await asyncio.sleep(2.0)
        finally:
            drainer.cancel()
            try:
                await drainer
            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        self._stopped = True
        self._flush_event.set()
        if self._out_q is not None:
            await self._out_q.put(None)


# ---------------------------------------------------------------------------
# Pub/Sub subscriber bridge (unchanged from HTTP version)
# ---------------------------------------------------------------------------

class Subscriber:
    def __init__(
        self, settings: Settings, forwarder: GrpcForwarder,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._s = settings
        self._fwd = forwarder
        self._loop = loop
        self._client = pubsub_v1.SubscriberClient()
        self._future = None

    def start(self) -> None:
        flow = pubsub_v1.types.FlowControl(
            max_messages=256, max_bytes=10 * 1024 * 1024,
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
        asyncio.run_coroutine_threadsafe(
            self._fwd.enqueue(det, message.ack, len(raw)), self._loop,
        )


# ---------------------------------------------------------------------------
# Stats / health HTTP -- Cloud Run expects something to bind to PORT
# ---------------------------------------------------------------------------

def _start_health_server(port: int, forwarder: GrpcForwarder) -> threading.Thread:
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
    forwarder = GrpcForwarder(settings)
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
        ],
    )
    settings = Settings.from_env()
    log.info(
        "egress_publisher_starting",
        receiver_target=settings.receiver_target,
        audience=settings.receiver_audience,
        batch_size=settings.batch_size,
        batch_timeout_s=settings.batch_timeout_s,
    )
    logging.getLogger().setLevel(logging.INFO)
    asyncio.run(_amain(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
