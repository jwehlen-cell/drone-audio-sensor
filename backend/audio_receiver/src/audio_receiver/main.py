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

    def __init__(self, stats: _Stats) -> None:
        self._stats = stats

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
                else:
                    # Unknown oneof -- still count the wire bytes so
                    # nothing gets lost from the receiver's totals.
                    with self._stats.lock:
                        self._stats.wire_bytes += wire_size

                # Minimal-cost ack so HTTP/2 flow control stays healthy
                # and the simulator can use server-side window updates.
                yield da_pb.ServerCommand()
        except grpc.RpcError as e:
            self._stats.record_stream_error()
            log.warning("stream_error peer=%s stream_id=%d err=%s",
                        peer, stream_id, e)
        finally:
            self._stats.forget_stream(stream_id)
            log.info("stream_close peer=%s stream_id=%d", peer, stream_id)


class AudioEgressStatsServicer(stats_grpc.AudioEgressStatsServicer):
    def __init__(self, stats: _Stats) -> None:
        self._stats = stats

    def GetStats(self, request, context):
        return self._stats.snapshot()

    def ResetTestRun(self, request, context):
        self._stats.reset(request.test_run_tag or "")
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
    da_grpc.add_DroneAudioStreamServicer_to_server(
        DroneAudioStreamServicer(stats), server,
    )
    stats_grpc.add_AudioEgressStatsServicer_to_server(
        AudioEgressStatsServicer(stats), server,
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
