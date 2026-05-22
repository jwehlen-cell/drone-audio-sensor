from __future__ import annotations

import time
import uuid
from typing import AsyncIterator

import grpc
import structlog

import drone_audio_pb2 as pb
import drone_audio_pb2_grpc as pb_grpc

from .auth import AuthError, JwtAuth
from .config import settings
from .registry import DeviceRegistry
from .state import DeviceState, DeviceStateStore

log = structlog.get_logger(__name__)


NETWORK_NAMES = {
    pb.NETWORK_TYPE_UNSPECIFIED: "unspecified",
    pb.NETWORK_TYPE_WIFI: "wifi",
    pb.NETWORK_TYPE_CELLULAR_LTE: "lte",
    pb.NETWORK_TYPE_CELLULAR_5G: "5g",
    pb.NETWORK_TYPE_CELLULAR_OTHER: "cellular",
    pb.NETWORK_TYPE_ETHERNET: "ethernet",
    pb.NETWORK_TYPE_NONE: "none",
}

THERMAL_NAMES = {
    pb.THERMAL_STATE_UNSPECIFIED: "unspecified",
    pb.THERMAL_STATE_NOMINAL: "nominal",
    pb.THERMAL_STATE_LIGHT: "light",
    pb.THERMAL_STATE_MODERATE: "moderate",
    pb.THERMAL_STATE_SEVERE: "severe",
    pb.THERMAL_STATE_CRITICAL: "critical",
    pb.THERMAL_STATE_EMERGENCY: "emergency",
}

LOCATION_NAMES = {
    pb.LOCATION_STATUS_UNSPECIFIED: "unspecified",
    pb.LOCATION_STATUS_CURRENT: "current",
    pb.LOCATION_STATUS_LAST_KNOWN: "last_known",
    pb.LOCATION_STATUS_MANUAL: "manual",
    pb.LOCATION_STATUS_UNAVAILABLE: "unavailable",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_command_id() -> str:
    return uuid.uuid4().hex


class DroneAudioStreamServicer(pb_grpc.DroneAudioStreamServicer):
    def __init__(
        self,
        registry: DeviceRegistry,
        state: DeviceStateStore,
        auth: JwtAuth | None = None,
    ) -> None:
        self._registry = registry
        self._state = state
        self._auth = auth

    async def StreamAudio(
        self,
        request_iterator: AsyncIterator[pb.ClientStreamMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb.ServerCommand]:
        peer = context.peer()
        bound = log.bind(peer=peer)

        authed_device_id: str | None = None
        if settings.require_auth:
            if self._auth is None:
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "auth required but not configured",
                )
                return
            token = _extract_bearer(context)
            try:
                claims = await self._auth.validate(token or "")
            except AuthError as e:
                bound.warning("auth_failed", reason=str(e))
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))
                return
            authed_device_id = str(claims.get("sub", ""))
            bound = bound.bind(authed_device_id=authed_device_id)

        try:
            first = await self._next_message(request_iterator)
        except StopAsyncIteration:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "stream ended before handshake")
            return

        if first.WhichOneof("payload") != "handshake":
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "first ClientStreamMessage must contain a handshake",
            )
            return

        handshake = first.handshake
        device_id = handshake.device_id
        if not device_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "device_id required")
            return

        if authed_device_id is not None and authed_device_id != device_id:
            bound.warning(
                "auth_device_id_mismatch",
                claimed=device_id,
                authenticated=authed_device_id,
            )
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "JWT subject does not match handshake device_id",
            )
            return

        session_id = _new_command_id()
        bound = bound.bind(device_id=device_id, session_id=session_id)
        bound.info("handshake_received", site_label=handshake.assigned_site_label)

        latitude = handshake.location.latitude if handshake.HasField("location") else None
        longitude = handshake.location.longitude if handshake.HasField("location") else None
        accuracy = (
            handshake.location.horizontal_accuracy_meters
            if handshake.HasField("location")
            else None
        )
        location_status = (
            LOCATION_NAMES.get(handshake.location.status, "unspecified")
            if handshake.HasField("location")
            else None
        )

        await self._registry.upsert_handshake(
            device_id=device_id,
            session_id=session_id,
            app_version=handshake.app_version,
            device_model=handshake.device_model,
            os_version=handshake.os_version,
            site_label=handshake.assigned_site_label,
            sample_rate_hz=handshake.sample_rate_hz,
            frame_duration_ms=handshake.frame_duration_ms,
            latitude=latitude,
            longitude=longitude,
            location_accuracy_m=accuracy,
            location_status=location_status,
            auth_token_id=handshake.auth_token_id,
        )

        initial_state = DeviceState(
            device_id=device_id,
            session_id=session_id,
            last_seen_ms=_now_ms(),
            last_sequence=-1,
            frames_received=0,
            dropped_frames=handshake.health.dropped_frames,
            reconnect_count=handshake.health.reconnect_count,
            app_version=handshake.app_version,
            network_type=NETWORK_NAMES.get(handshake.health.network_type, "unspecified"),
            battery_percent=handshake.health.battery_percent,
            thermal_state=THERMAL_NAMES.get(handshake.health.thermal_state, "unspecified"),
            site_label=handshake.assigned_site_label,
        )
        await self._state.init_session(initial_state)

        yield pb.ServerCommand(
            command_id=_new_command_id(),
            server_timestamp_ms=_now_ms(),
            session_ack=pb.SessionAck(
                device_id=device_id,
                assigned_session_id=session_id,
                ack_interval_frames=settings.ack_interval_frames,
                health_report_interval_seconds=settings.health_report_interval_seconds,
            ),
        )

        frames_received = 0
        last_acked_seq = -1
        disconnect_reason = "client_closed"
        try:
            async for message in request_iterator:
                kind = message.WhichOneof("payload")
                if kind == "audio_frame":
                    frame = message.audio_frame
                    frames_received += 1
                    await self._state.touch(
                        device_id,
                        sequence=int(frame.sequence_number),
                        frames_received=frames_received,
                    )
                    await self._state.publish_frame(
                        device_id=device_id,
                        session_id=session_id,
                        sequence=int(frame.sequence_number),
                        capture_timestamp_ms=int(frame.capture_timestamp_ms),
                        sample_rate_hz=int(frame.sample_rate_hz),
                        pcm16_mono=frame.pcm16_mono,
                        site_label=handshake.assigned_site_label,
                    )
                    if (
                        int(frame.sequence_number) - last_acked_seq
                        >= settings.ack_interval_frames
                    ):
                        last_acked_seq = int(frame.sequence_number)
                        yield pb.ServerCommand(
                            command_id=_new_command_id(),
                            server_timestamp_ms=_now_ms(),
                            frame_ack=pb.FrameAck(acked_through_sequence=last_acked_seq),
                        )
                elif kind == "health":
                    h = message.health
                    await self._state.update_health(
                        device_id,
                        dropped_frames=h.dropped_frames,
                        reconnect_count=h.reconnect_count,
                        network_type=NETWORK_NAMES.get(h.network_type, "unspecified"),
                        battery_percent=h.battery_percent,
                        thermal_state=THERMAL_NAMES.get(h.thermal_state, "unspecified"),
                    )
                elif kind == "location_update":
                    loc = message.location_update.location
                    await self._registry.update_location(
                        device_id,
                        latitude=loc.latitude,
                        longitude=loc.longitude,
                        accuracy_m=loc.horizontal_accuracy_meters,
                        status=LOCATION_NAMES.get(loc.status, "unspecified"),
                        timestamp_ms=int(loc.location_timestamp_ms),
                    )
                elif kind == "handshake":
                    bound.warning("unexpected_second_handshake_ignored")
                else:
                    bound.debug("unknown_payload", kind=kind)
        except grpc.aio.AioRpcError as e:
            disconnect_reason = f"rpc_error:{e.code().name}"
            bound.warning("stream_rpc_error", code=e.code().name, detail=str(e.details()))
        except Exception as e:  # noqa: BLE001
            disconnect_reason = f"exception:{type(e).__name__}"
            bound.exception("stream_failed")
        finally:
            await self._registry.mark_disconnected(device_id, reason=disconnect_reason)
            bound.info(
                "stream_closed",
                frames_received=frames_received,
                reason=disconnect_reason,
            )

    @staticmethod
    async def _next_message(
        request_iterator: AsyncIterator[pb.ClientStreamMessage],
    ) -> pb.ClientStreamMessage:
        async for msg in request_iterator:
            return msg
        raise StopAsyncIteration


def _extract_bearer(context: grpc.aio.ServicerContext) -> str | None:
    for key, value in context.invocation_metadata():
        if key.lower() == "authorization" and value.lower().startswith("bearer "):
            return value[7:].strip()
    return None
