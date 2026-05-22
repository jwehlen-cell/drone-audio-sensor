"""Behavioral checks on the StreamAudio dispatch.

We don't spin up a real gRPC server here; instead we exercise the
servicer with an async iterator of stream messages and a stub registry/
state store, and assert the gateway-internal effects (publish_frame
called or not, mark_wipe_sent called or not).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the in-image generated proto stubs can be imported when running
# tests with PYTHONPATH=src:proto_gen.
pytest.importorskip("drone_audio_pb2")

import drone_audio_pb2 as pb  # noqa: E402

from gateway import service as service_mod  # noqa: E402
from gateway.service import DroneAudioStreamServicer  # noqa: E402
from gateway.state_machine import (  # noqa: E402
    STATE_ACTIVE,
    STATE_LOST,
    STATE_WIPE_REQUESTED,
)


def _msg_handshake(device_id: str = "TEST-DEV-1") -> pb.ClientStreamMessage:
    h = pb.ConnectHandshake(device_id=device_id, sample_rate_hz=16000, frame_duration_ms=1000)
    return pb.ClientStreamMessage(handshake=h)


def _msg_audio(device_id: str = "TEST-DEV-1", seq: int = 1) -> pb.ClientStreamMessage:
    af = pb.AudioFrame(
        device_id=device_id,
        sequence_number=seq,
        capture_timestamp_ms=1,
        sample_rate_hz=16000,
        pcm16_mono=b"\x00" * 32,
    )
    return pb.ClientStreamMessage(audio_frame=af)


async def _iter(messages: list[pb.ClientStreamMessage]) -> AsyncIterator[pb.ClientStreamMessage]:
    for m in messages:
        yield m


class _FakeContext:
    """Minimal grpc.aio.ServicerContext stand-in."""

    def __init__(self) -> None:
        self.aborted: tuple[object, str] | None = None

    def peer(self) -> str:
        return "ipv4:127.0.0.1:0"

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return []

    async def abort(self, code, details):  # noqa: ANN001
        self.aborted = (code, details)
        raise StopAsyncIteration


def _build_servicer(state: str) -> tuple[DroneAudioStreamServicer, MagicMock, MagicMock]:
    registry = MagicMock()
    registry.upsert_handshake = AsyncMock()
    registry.get_state = AsyncMock(return_value=state)
    registry.mark_wipe_sent = AsyncMock()
    registry.mark_disconnected = AsyncMock()
    registry.update_location = AsyncMock()

    store = MagicMock()
    store.init_session = AsyncMock()
    store.touch = AsyncMock()
    store.publish_frame = AsyncMock()
    store.update_health = AsyncMock()

    servicer = DroneAudioStreamServicer(registry, store, auth=None)
    return servicer, registry, store


async def _collect(generator) -> list:
    out = []
    try:
        async for v in generator:
            out.append(v)
    except StopAsyncIteration:
        pass
    return out


@pytest.mark.asyncio
async def test_active_device_publishes_audio() -> None:
    # require_auth defaults to False in test environment, so the auth check is skipped.
    service_mod.settings.require_auth = False
    servicer, _registry, store = _build_servicer(STATE_ACTIVE)
    ctx = _FakeContext()
    requests = _iter([_msg_handshake(), _msg_audio(seq=5)])
    await _collect(servicer.StreamAudio(requests, ctx))
    assert store.publish_frame.await_count == 1
    args, kwargs = store.publish_frame.await_args
    assert kwargs["sequence"] == 5


@pytest.mark.asyncio
async def test_lost_device_skips_audio_publish() -> None:
    service_mod.settings.require_auth = False
    servicer, _registry, store = _build_servicer(STATE_LOST)
    ctx = _FakeContext()
    requests = _iter([_msg_handshake(), _msg_audio(seq=1), _msg_audio(seq=2)])
    await _collect(servicer.StreamAudio(requests, ctx))
    # Touch is still called so the dashboard shows liveness;
    # publish is NEVER called for a lost device.
    assert store.touch.await_count == 2
    assert store.publish_frame.await_count == 0


@pytest.mark.asyncio
async def test_wipe_requested_dispatches_wipe_and_marks_sent() -> None:
    service_mod.settings.require_auth = False
    servicer, registry, store = _build_servicer(STATE_WIPE_REQUESTED)
    ctx = _FakeContext()
    # Audio frames after the handshake should be ignored — the wipe path
    # returns immediately after dispatching the control message.
    requests = _iter([_msg_handshake(), _msg_audio(seq=1)])
    yielded = await _collect(servicer.StreamAudio(requests, ctx))
    control_msgs = [m for m in yielded if m.HasField("control")]
    assert len(control_msgs) == 1
    assert control_msgs[0].control.type == pb.CONTROL_TYPE_WIPE_DEVICE
    assert registry.mark_wipe_sent.await_count == 1
    # No audio frames published from a wipe_requested device.
    assert store.publish_frame.await_count == 0
