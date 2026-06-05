"""Cross-project AUDIO egress receiver. Drops every frame, keeps
byte stats only.

Runs in drone-audio-sensor (us-west2). The simulator in argosuat
opens one persistent gRPC bidi stream per simulated phone to the
gateway (production path) AND a parallel stream to this service
(test-only). Each frame is counted, immediately dropped, and never
retained beyond the in-memory accumulators.

This service implements TWO gRPC services on one port:

  drone.audio.DroneAudioStream  (from proto/drone_audio.proto)
    StreamAudio(stream ClientStreamMessage) returns (stream ServerCommand)
      -- the same RPC the gateway exposes. We accept the simulator's
         frames so it doesn't need a different client codepath; we
         drain them, count bytes, and reply with empty ServerCommand
         acks so HTTP/2 flow control stays healthy. Audio payload
         bytes are never written anywhere -- they live for the
         duration of the protobuf decode and are GC'd as soon as the
         counters update.

  drone.audio_egress.AudioEgressStats  (from proto/audio_egress_stats.proto)
    GetStats(StatsRequest) returns (AudioStats)
    ResetTestRun(ResetRequest) returns (AudioStats)
      -- the sidecar pull endpoint. Lets an operator tag a fresh
         test run and pull totals without redeploying.

Why both services in one process:
  * one Cloud Run instance instead of two
  * the stats sidecar reads the same in-process counters that the
    StreamAudio servicer increments, no cross-service coordination
  * gRPC server with multiple services is the natural pattern

Why not just decode the audio + measure PCM bytes:
  * we don't decode FLAC -- that would burn CPU without changing
    the wire bytes the receiver actually saw. The PCM-equivalent
    stat is computed from sample_rate_hz + frame_duration_ms in
    the handshake, so codecs can be compared without re-decoding.
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
from google.cloud import firestore  # type: ignore

import drone_audio_pb2 as da_pb  # type: ignore
import drone_audio_pb2_grpc as da_grpc  # type: ignore
import audio_egress_stats_pb2 as stats_pb  # type: ignore
import audio_egress_stats_pb2_grpc as stats_grpc  # type: ignore

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("audio_receiver")


_CODEC_TO_FIELD = {
    "pcm16": "frames_pcm16",
    "wav": "frames_wav",
    "flac": "frames_flac",
}

# Firestore wiring: every receiver instance keeps an in-memory delta
# accumulator + flushes it via atomic Increment() ops every
# DELTA_FLUSH_SECONDS. This makes counters multi-instance safe (no
# more lost data when Cloud Run scales out) AND persists them to the
# test_status dashboard.
FIRESTORE_COLLECTION = "test_runs"
DELTA_FLUSH_SECONDS = 30
RECEIVER_TYPE = "audio_egress"
# A run is marked "complete" when no frames have arrived in this long.
STALE_RUN_SECONDS = 300


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Stats:
    """Per-test-run accumulators. Thread-safe.

    All counters are scalars; no per-message data is retained beyond
    the moment the StreamAudio servicer increments the counters. The
    incoming protobuf object is released immediately after the
    accumulators update, so the audio bytes are GC-collectible by
    the time the next frame arrives.
    """

    __slots__ = (
        "lock", "test_run_tag",
        "handshakes_received", "frames_received",
        "wire_bytes", "audio_payload_bytes", "pcm_equivalent_bytes",
        "frames_pcm16", "frames_wav", "frames_flac", "frames_unknown_codec",
        "max_single_frame_bytes", "stream_errors",
        "started_at_unix_ms", "last_frame_at_unix_ms",
        # Per-stream cached handshake context, keyed by stream id, used
        # to derive PCM-equivalent bytes from later AudioFrames on the
        # same stream. AudioFrame carries sample_rate_hz but not
        # frame_duration_ms; the handshake has both.
        "_per_stream_pcm_per_frame",
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.test_run_tag = ""
        self.handshakes_received = 0
        self.frames_received = 0
        self.wire_bytes = 0
        self.audio_payload_bytes = 0
        self.pcm_equivalent_bytes = 0
        self.frames_pcm16 = 0
        self.frames_wav = 0
        self.frames_flac = 0
        self.frames_unknown_codec = 0
        self.max_single_frame_bytes = 0
        self.stream_errors = 0
        self.started_at_unix_ms = _now_ms()
        self.last_frame_at_unix_ms = 0
        self._per_stream_pcm_per_frame: dict[int, int] = {}

    def reset(self, tag: str) -> None:
        with self.lock:
            self.test_run_tag = tag
            self.handshakes_received = 0
            self.frames_received = 0
            self.wire_bytes = 0
            self.audio_payload_bytes = 0
            self.pcm_equivalent_bytes = 0
            self.frames_pcm16 = 0
            self.frames_wav = 0
            self.frames_flac = 0
            self.frames_unknown_codec = 0
            self.max_single_frame_bytes = 0
            self.stream_errors = 0
            self.started_at_unix_ms = _now_ms()
            self.last_frame_at_unix_ms = 0
            self._per_stream_pcm_per_frame.clear()
        log.info("test_run_reset tag=%s", tag)

    def record_handshake(
        self, stream_id: int, wire_size: int,
        sample_rate_hz: int, frame_duration_ms: int,
    ) -> None:
        # PCM bytes per frame = (sample_rate_hz * frame_duration_ms /
        # 1000) samples * 2 bytes/sample (16-bit mono).
        pcm_per_frame = max(
            0, int(sample_rate_hz * frame_duration_ms / 1000) * 2,
        )
        with self.lock:
            self.handshakes_received += 1
            self.wire_bytes += wire_size
            if wire_size > self.max_single_frame_bytes:
                self.max_single_frame_bytes = wire_size
            self._per_stream_pcm_per_frame[stream_id] = pcm_per_frame

    def record_frame(
        self, stream_id: int, wire_size: int,
        audio_bytes_len: int, codec: str,
    ) -> None:
        with self.lock:
            self.frames_received += 1
            self.wire_bytes += wire_size
            self.audio_payload_bytes += audio_bytes_len
            field = _CODEC_TO_FIELD.get(codec, "frames_unknown_codec")
            setattr(self, field, getattr(self, field) + 1)
            pcm_per_frame = self._per_stream_pcm_per_frame.get(stream_id, 0)
            self.pcm_equivalent_bytes += pcm_per_frame
            if wire_size > self.max_single_frame_bytes:
                self.max_single_frame_bytes = wire_size
            self.last_frame_at_unix_ms = _now_ms()

    def record_stream_error(self) -> None:
        with self.lock:
            self.stream_errors += 1

    def forget_stream(self, stream_id: int) -> None:
        with self.lock:
            self._per_stream_pcm_per_frame.pop(stream_id, None)

    def snapshot(self) -> stats_pb.AudioStats:
        with self.lock:
            return stats_pb.AudioStats(
                test_run_tag=self.test_run_tag,
                handshakes_received=self.handshakes_received,
                frames_received=self.frames_received,
                wire_bytes=self.wire_bytes,
                audio_payload_bytes=self.audio_payload_bytes,
                pcm_equivalent_bytes=self.pcm_equivalent_bytes,
                frames_pcm16=self.frames_pcm16,
                frames_wav=self.frames_wav,
                frames_flac=self.frames_flac,
                frames_unknown_codec=self.frames_unknown_codec,
                max_single_frame_bytes=self.max_single_frame_bytes,
                stream_errors=self.stream_errors,
                started_at_unix_ms=self.started_at_unix_ms,
                last_frame_at_unix_ms=self.last_frame_at_unix_ms,
            )


class _FirestoreSink:
    """Periodically flushes per-test-run counter deltas to Firestore
    via atomic Increment(). Lets the dashboard show a consistent
    cross-instance total even when Cloud Run scales out the receiver.

    Each receiver instance:
      - Accumulates deltas in-memory (cheap, no per-frame Firestore I/O)
      - Flushes every DELTA_FLUSH_SECONDS to drone-audio-sensor's
        test_runs/<test_run_tag> doc via FieldValue.increment
      - Resets local deltas on flush
      - On ResetTestRun: writes a fresh run doc with status="running"
        AND marks any previously-running tag as "complete"
    """

    def __init__(self) -> None:
        try:
            self._db = firestore.Client()
        except Exception as e:  # noqa: BLE001
            log.warning("firestore_init_failed err=%s; running without persistence", e)
            self._db = None
        self._lock = threading.Lock()
        # Local delta accumulator. Flushed + reset every cycle.
        self._delta: dict[str, int] = {}
        self._max_single = 0
        self._test_run_tag = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._db is None:
            return
        self._thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="firestore-flush",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # One last flush so end-of-test counters land.
        if self._db is not None:
            self._flush_once(final=True)

    def reset_run(self, tag: str) -> None:
        """Called from ResetTestRun. Marks any previously-running doc
        as complete, then creates a fresh doc for this tag."""
        if self._db is None:
            self._test_run_tag = tag
            return
        # Flush whatever's left for the previous tag, then close it.
        self._flush_once(final=True, finalize_tag=self._test_run_tag)
        with self._lock:
            self._delta.clear()
            self._max_single = 0
            self._test_run_tag = tag
        try:
            self._db.collection(FIRESTORE_COLLECTION).document(tag).set({
                "test_run_tag": tag,
                "receiver_type": RECEIVER_TYPE,
                "status": "running",
                "started_at": firestore.SERVER_TIMESTAMP,
                "last_updated_at": firestore.SERVER_TIMESTAMP,
                "ended_at": None,
                "handshakes_received": 0,
                "frames_received": 0,
                "wire_bytes": 0,
                "audio_payload_bytes": 0,
                "pcm_equivalent_bytes": 0,
                "frames_pcm16": 0,
                "frames_wav": 0,
                "frames_flac": 0,
                "frames_unknown_codec": 0,
                "max_single_frame_bytes": 0,
                "stream_errors": 0,
            })
            log.info("firestore_run_created tag=%s", tag)
        except Exception as e:  # noqa: BLE001
            log.warning("firestore_run_create_failed tag=%s err=%s", tag, e)

    def record_delta(self, **deltas: int) -> None:
        """Bump local accumulators. Called from the gRPC servicer on
        every frame/handshake/error."""
        max_frame = deltas.pop("_max_single", 0)
        with self._lock:
            for k, v in deltas.items():
                self._delta[k] = self._delta.get(k, 0) + v
            if max_frame > self._max_single:
                self._max_single = max_frame

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(timeout=DELTA_FLUSH_SECONDS)
            if self._stop.is_set():
                break
            self._flush_once()
            self._sweep_stale()

    def _flush_once(self, final: bool = False, finalize_tag: str = "") -> None:
        if self._db is None:
            return
        with self._lock:
            tag_to_flush = finalize_tag or self._test_run_tag
            if not tag_to_flush:
                return
            deltas = dict(self._delta)
            max_frame = self._max_single
            self._delta.clear()
            # Don't reset _max_single -- the doc-side update uses
            # max(old, new) via a transaction so the local watermark
            # being remembered across flushes is harmless.

        if not deltas and max_frame == 0 and not (final and finalize_tag):
            # Nothing to write this cycle.
            return

        try:
            update: dict = {"last_updated_at": firestore.SERVER_TIMESTAMP}
            for k, v in deltas.items():
                if v:
                    update[k] = firestore.Increment(v)
            doc = self._db.collection(FIRESTORE_COLLECTION).document(tag_to_flush)
            if max_frame:
                # max() across instances: read-modify-write in a
                # transaction. Cheap (one doc).
                @firestore.transactional
                def _bump_max(tx, ref):
                    snap = ref.get(transaction=tx)
                    cur = (snap.to_dict() or {}).get("max_single_frame_bytes", 0)
                    if max_frame > cur:
                        tx.update(ref, {"max_single_frame_bytes": max_frame})
                _bump_max(self._db.transaction(), doc)
            if final and finalize_tag:
                update["status"] = "complete"
                update["ended_at"] = firestore.SERVER_TIMESTAMP
            if update:
                doc.update(update)
        except Exception as e:  # noqa: BLE001
            log.warning("firestore_flush_failed tag=%s err=%s", tag_to_flush, e)

    def _sweep_stale(self) -> None:
        """Mark long-idle 'running' runs as 'complete'. Cheap query
        scoped to this receiver_type so multiple receivers' docs
        don't step on each other."""
        if self._db is None:
            return
        try:
            now = time.time()
            stale_cutoff_ms = int((now - STALE_RUN_SECONDS) * 1000)
            q = (
                self._db.collection(FIRESTORE_COLLECTION)
                .where("receiver_type", "==", RECEIVER_TYPE)
                .where("status", "==", "running")
                .limit(20)
            )
            for snap in q.stream():
                d = snap.to_dict() or {}
                last_updated = d.get("last_updated_at")
                if last_updated is None:
                    continue
                last_ms = int(last_updated.timestamp() * 1000)
                if last_ms < stale_cutoff_ms:
                    snap.reference.update({
                        "status": "complete",
                        "ended_at": firestore.SERVER_TIMESTAMP,
                    })
                    log.info("firestore_run_auto_completed tag=%s", snap.id)
        except Exception as e:  # noqa: BLE001
            log.warning("firestore_sweep_failed err=%s", e)


# Atomic-ish stream id generator. We only need per-stream uniqueness
# within a single receiver process; uint64 is plenty.
_NEXT_STREAM_ID = 0
_STREAM_ID_LOCK = threading.Lock()


def _next_stream_id() -> int:
    global _NEXT_STREAM_ID
    with _STREAM_ID_LOCK:
        _NEXT_STREAM_ID += 1
        return _NEXT_STREAM_ID


class DroneAudioStreamServicer(da_grpc.DroneAudioStreamServicer):
    """Drop-and-count implementation of the gateway-side audio RPC.

    The simulator opens one of these streams per phone (in addition
    to its real stream to the gateway). Each ClientStreamMessage --
    handshake or audio_frame -- gets its size counted, its data field
    referenced just long enough to read len(), then released.
    """

    def __init__(self, stats: _Stats, sink: _FirestoreSink) -> None:
        self._stats = stats
        self._sink = sink

    def StreamAudio(self, request_iterator, context):
        stream_id = _next_stream_id()
        peer = context.peer() if context else "<unknown>"
        log.info("stream_open peer=%s stream_id=%d", peer, stream_id)
        try:
            for msg in request_iterator:
                wire_size = msg.ByteSize()
                kind = msg.WhichOneof("payload")
                if kind == "handshake":
                    hs = msg.handshake
                    self._stats.record_handshake(
                        stream_id=stream_id,
                        wire_size=wire_size,
                        sample_rate_hz=hs.sample_rate_hz,
                        frame_duration_ms=hs.frame_duration_ms,
                    )
                    self._sink.record_delta(
                        handshakes_received=1, wire_bytes=wire_size,
                        _max_single=wire_size,
                    )
                elif kind == "audio_frame":
                    af = msg.audio_frame
                    # len() forces the bytes object to exist; we don't
                    # touch the contents, so the bytes are GC'd as
                    # soon as the loop iterates again.
                    audio_len = len(af.pcm16_mono)
                    codec = af.codec or "pcm16"
                    self._stats.record_frame(
                        stream_id=stream_id,
                        wire_size=wire_size,
                        audio_bytes_len=audio_len,
                        codec=codec,
                    )
                    codec_field = _CODEC_TO_FIELD.get(codec, "frames_unknown_codec")
                    # PCM-equivalent: derive from the stream's cached
                    # handshake context the same way record_frame does
                    # for the in-memory stat. Look it up under the
                    # stats lock to be safe.
                    with self._stats.lock:
                        pcm_per_frame = self._stats._per_stream_pcm_per_frame.get(stream_id, 0)
                    self._sink.record_delta(
                        frames_received=1,
                        wire_bytes=wire_size,
                        audio_payload_bytes=audio_len,
                        pcm_equivalent_bytes=pcm_per_frame,
                        **{codec_field: 1},
                        _max_single=wire_size,
                    )
                else:
                    # Unknown oneof -- still count the wire bytes so
                    # nothing gets lost from the receiver's totals.
                    with self._stats.lock:
                        self._stats.wire_bytes += wire_size
                    self._sink.record_delta(wire_bytes=wire_size)

                # Minimal-cost ack so HTTP/2 flow control stays healthy
                # and the simulator can use server-side window updates.
                yield da_pb.ServerCommand()
        except grpc.RpcError as e:
            self._stats.record_stream_error()
            self._sink.record_delta(stream_errors=1)
            log.warning("stream_error peer=%s stream_id=%d err=%s",
                        peer, stream_id, e)
        finally:
            self._stats.forget_stream(stream_id)
            log.info("stream_close peer=%s stream_id=%d", peer, stream_id)


class AudioEgressStatsServicer(stats_grpc.AudioEgressStatsServicer):
    def __init__(self, stats: _Stats, sink: _FirestoreSink) -> None:
        self._stats = stats
        self._sink = sink

    def GetStats(self, request, context):
        return self._stats.snapshot()

    def ResetTestRun(self, request, context):
        tag = request.test_run_tag or ""
        self._stats.reset(tag)
        self._sink.reset_run(tag)
        return self._stats.snapshot()


def serve() -> int:
    port = int(os.environ.get("PORT", "8080"))
    max_workers = int(os.environ.get("MAX_WORKERS", "32"))
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            # Audio frames are big (50-300 KB/FLAC, MB-range PCM). Bump
            # well above the default 4 MiB so pathological frames don't
            # reset the stream.
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            # Keepalive ensures Cloud Run keeps the stream warm under
            # quiet periods (rare with 1,000 phones at 5 s cadence).
            ("grpc.keepalive_time_ms", 300_000),
            ("grpc.keepalive_timeout_ms", 20_000),
            ("grpc.keepalive_permit_without_calls", 1),
        ],
    )
    stats = _Stats()
    sink = _FirestoreSink()
    sink.start()
    da_grpc.add_DroneAudioStreamServicer_to_server(
        DroneAudioStreamServicer(stats, sink), server,
    )
    stats_grpc.add_AudioEgressStatsServicer_to_server(
        AudioEgressStatsServicer(stats, sink), server,
    )
    server.add_insecure_port(f"[::]:{port}")
    log.info(
        "starting audio_receiver port=%d max_workers=%d",
        port, max_workers,
    )
    server.start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def _report() -> None:
        while not stop.is_set():
            stop.wait(timeout=60)
            s = stats.snapshot()
            log.info(
                "stats tag=%s handshakes=%d frames=%d wire=%d audio=%d pcm_eq=%d "
                "flac=%d pcm=%d wav=%d unk=%d max=%d errs=%d",
                s.test_run_tag,
                s.handshakes_received, s.frames_received,
                s.wire_bytes, s.audio_payload_bytes, s.pcm_equivalent_bytes,
                s.frames_flac, s.frames_pcm16, s.frames_wav,
                s.frames_unknown_codec,
                s.max_single_frame_bytes, s.stream_errors,
            )

    threading.Thread(target=_report, daemon=True).start()
    stop.wait()
    server.stop(grace=10).wait()
    sink.stop()
    final = stats.snapshot()
    log.info(
        "final tag=%s frames=%d wire=%d audio=%d pcm_eq=%d",
        final.test_run_tag,
        final.frames_received,
        final.wire_bytes,
        final.audio_payload_bytes,
        final.pcm_equivalent_bytes,
    )
    return 0


if __name__ == "__main__":
    sys.exit(serve())
