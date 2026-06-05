"""Cross-project egress receiver. Drops every payload, keeps stats
only -- the receiver's job is to measure what the publisher would
have sent to a real downstream system, not to do anything with the
data.

Runs in the drone-audio-sensor GCP project. The argosuat
egress_publisher opens one bidirectional gRPC stream and streams
EgressBatch messages over it; this server acks each batch (so the
publisher can advance its Pub/Sub ack) and accumulates per-test-run
totals in memory.

Why gRPC and not HTTP for this receiver:
  * one persistent stream per publisher instance instead of one
    HTTP POST per batch -- amortizes TLS handshake + HTTP/2 setup
    across the whole test
  * Cloud Run bills per request; gRPC bidi = 1 request per stream
    lifetime vs 1 request per batch under HTTP, so the receiver's
    Cloud Run request charges are roughly fixed regardless of how
    many detections move through it
  * matches the production phone-app shape (persistent streams) so
    publisher + receiver code uses the same primitives

Stats reset endpoints let the operator tag a new test run without
redeploying. Old data is dropped at reset, so successive measurement
windows do not contaminate each other.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from concurrent import futures

import grpc

import egress_batch_pb2 as eb_pb  # type: ignore
import egress_service_pb2 as svc_pb  # type: ignore
import egress_service_pb2_grpc as svc_grpc  # type: ignore

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("egress_receiver")


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Stats:
    """Per-test-run accumulators. All counters are reset by
    ResetTestRun; the receiver never persists payloads or per-batch
    data beyond these scalars (and a one-batch peak)."""

    __slots__ = (
        "lock",
        "test_run_tag",
        "batches_received",
        "detections_received",
        "wire_bytes_received",
        "proto_bytes_received",
        "errors",
        "max_single_batch_bytes",
        "started_at_unix_ms",
        "last_batch_at_unix_ms",
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.test_run_tag = ""
        self.batches_received = 0
        self.detections_received = 0
        self.wire_bytes_received = 0
        self.proto_bytes_received = 0
        self.errors = 0
        self.max_single_batch_bytes = 0
        self.started_at_unix_ms = _now_ms()
        self.last_batch_at_unix_ms = 0

    def reset(self, tag: str) -> None:
        with self.lock:
            self.test_run_tag = tag
            self.batches_received = 0
            self.detections_received = 0
            self.wire_bytes_received = 0
            self.proto_bytes_received = 0
            self.errors = 0
            self.max_single_batch_bytes = 0
            self.started_at_unix_ms = _now_ms()
            self.last_batch_at_unix_ms = 0
        log.info("test_run_reset tag=%s", tag)

    def record_batch(self, proto_bytes: int, detections: int) -> None:
        with self.lock:
            self.batches_received += 1
            self.detections_received += detections
            # proto_bytes is the serialized EgressBatch size as the
            # server saw it post-gRPC-unframing. wire_bytes_received
            # tracks the same thing in aggregate -- gRPC compression
            # is left to the channel layer, and we don't enable it
            # here because the test setup will measure raw + add gzip
            # at the application level when it wants to compare.
            self.wire_bytes_received += proto_bytes
            self.proto_bytes_received += proto_bytes
            if proto_bytes > self.max_single_batch_bytes:
                self.max_single_batch_bytes = proto_bytes
            self.last_batch_at_unix_ms = _now_ms()

    def snapshot(self) -> svc_pb.Stats:
        with self.lock:
            return svc_pb.Stats(
                test_run_tag=self.test_run_tag,
                batches_received=self.batches_received,
                detections_received=self.detections_received,
                wire_bytes_received=self.wire_bytes_received,
                proto_bytes_received=self.proto_bytes_received,
                errors=self.errors,
                max_single_batch_bytes=self.max_single_batch_bytes,
                started_at_unix_ms=self.started_at_unix_ms,
                last_batch_at_unix_ms=self.last_batch_at_unix_ms,
            )


class EgressReceiverServicer(svc_grpc.EgressReceiverServicer):
    def __init__(self, stats: _Stats) -> None:
        self._stats = stats

    def StreamBatches(self, request_iterator, context):
        """One bidi stream per publisher instance. Each EgressBatch
        is counted + dropped; a BatchAck flows back so the publisher
        can advance Pub/Sub acks only after the receiver has seen
        the bytes."""
        seq = 0
        peer = context.peer() if context else "<unknown>"
        log.info("stream_open peer=%s", peer)
        try:
            for batch in request_iterator:
                seq += 1
                size = batch.ByteSize()
                n_det = len(batch.detections)
                self._stats.record_batch(size, n_det)
                yield svc_pb.BatchAck(
                    batch_seq=seq, server_received_bytes=size,
                )
        except grpc.RpcError as e:
            with self._stats.lock:
                self._stats.errors += 1
            log.warning("stream_error peer=%s err=%s", peer, e)
        finally:
            log.info("stream_close peer=%s batches=%d", peer, seq)

    def GetStats(self, request, context):
        return self._stats.snapshot()

    def ResetTestRun(self, request, context):
        self._stats.reset(request.test_run_tag or "")
        return self._stats.snapshot()


def serve() -> int:
    port = int(os.environ.get("PORT", "8080"))
    max_workers = int(os.environ.get("MAX_WORKERS", "16"))
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            # Cloud Run terminates idle HTTP/2 connections at ~15 min;
            # send keepalive pings every 5 min to keep the publisher's
            # stream healthy under low-traffic conditions.
            ("grpc.keepalive_time_ms", 300_000),
            ("grpc.keepalive_timeout_ms", 20_000),
            ("grpc.keepalive_permit_without_calls", 1),
            # Bump default 4 MiB cap so a pathologically large batch
            # still gets through (and counted) without resetting the
            # stream.
            ("grpc.max_receive_message_length", 32 * 1024 * 1024),
        ],
    )
    stats = _Stats()
    svc_grpc.add_EgressReceiverServicer_to_server(
        EgressReceiverServicer(stats), server,
    )
    server.add_insecure_port(f"[::]:{port}")
    log.info("starting egress_receiver port=%d max_workers=%d", port, max_workers)
    server.start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    # Periodic log line so an operator can spot-check what the
    # receiver has seen without going through gRPC GetStats.
    def _report() -> None:
        while not stop.is_set():
            stop.wait(timeout=60)
            s = stats.snapshot()
            log.info(
                "stats tag=%s batches=%d detections=%d wire=%d max_batch=%d",
                s.test_run_tag,
                s.batches_received,
                s.detections_received,
                s.wire_bytes_received,
                s.max_single_batch_bytes,
            )

    threading.Thread(target=_report, daemon=True).start()

    stop.wait()
    server.stop(grace=10).wait()
    final = stats.snapshot()
    log.info(
        "final tag=%s batches=%d detections=%d wire=%d",
        final.test_run_tag,
        final.batches_received,
        final.detections_received,
        final.wire_bytes_received,
    )
    return 0


if __name__ == "__main__":
    sys.exit(serve())
