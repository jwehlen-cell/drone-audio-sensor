package com.dronesensor.app.stream

import android.content.Context
import android.os.Build
import android.util.Log
import com.dronesensor.app.BuildConfig
import com.dronesensor.app.admin.WipeHandler
import com.dronesensor.app.audio.AudioFrameProducer
import com.dronesensor.app.audio.CapturedFrame
import com.dronesensor.app.config.AppConfig
import com.dronesensor.app.health.DeviceHealthSnapshot
import com.dronesensor.app.identity.DeviceIdentity
import com.dronesensor.app.identity.JwtSigner
import com.dronesensor.app.location.LocationProvider
import com.dronesensor.proto.AudioFrame
import com.dronesensor.proto.ClientStreamMessage
import com.dronesensor.proto.ConnectHandshake
import com.dronesensor.proto.ControlType
import com.dronesensor.proto.DeviceHealth
import com.dronesensor.proto.DeviceLocation
import com.dronesensor.proto.DroneAudioStreamGrpcKt
import com.dronesensor.proto.LocationStatus
import com.dronesensor.proto.LocationUpdate
import com.dronesensor.proto.ServerCommand
import com.google.protobuf.ByteString
import io.grpc.ClientInterceptors
import io.grpc.ManagedChannel
import io.grpc.Metadata
import io.grpc.okhttp.OkHttpChannelBuilder
import io.grpc.stub.MetadataUtils
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.concurrent.TimeUnit

enum class StreamState { IDLE, CONNECTING, STREAMING, RECONNECTING, ERROR }

data class StreamMetrics(
    val framesSent: Long = 0,
    val droppedFrames: Int = 0,
    val reconnects: Int = 0,
    val lastAckTimestampMs: Long = 0,
    val lastAckedSequence: Long = -1
)

class StreamingClient(
    private val context: Context,
    private val producer: AudioFrameProducer,
    private val locationProvider: LocationProvider? = null
) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var runJob: Job? = null
    private val reconnectPolicy = ReconnectPolicy()

    private val _state = MutableStateFlow(StreamState.IDLE)
    val state: StateFlow<StreamState> = _state.asStateFlow()

    private val _metrics = MutableStateFlow(StreamMetrics())
    val metrics: StateFlow<StreamMetrics> = _metrics.asStateFlow()

    private val outboundExtras = Channel<ClientStreamMessage>(
        capacity = 32,
        onBufferOverflow = BufferOverflow.DROP_OLDEST
    )

    fun start() {
        if (runJob?.isActive == true) return
        runJob = scope.launch { runLoop() }
    }

    fun stop() {
        runJob?.cancel()
        runJob = null
        _state.value = StreamState.IDLE
    }

    fun submitHealth(health: DeviceHealth) {
        outboundExtras.trySend(
            ClientStreamMessage.newBuilder().setHealth(health).build()
        )
    }

    fun submitLocationUpdate(location: DeviceLocation) {
        if (location.status == LocationStatus.LOCATION_STATUS_UNAVAILABLE) return
        val update = LocationUpdate.newBuilder()
            .setDeviceId(DeviceIdentity.get(context).deviceId)
            .setUpdateTimestampMs(System.currentTimeMillis())
            .setLocation(location)
            .build()
        outboundExtras.trySend(
            ClientStreamMessage.newBuilder().setLocationUpdate(update).build()
        )
    }

    private suspend fun runLoop() {
        while (scope.isActive) {
            val endpoint = AppConfig.get(context).endpoint
            var channel: ManagedChannel? = null
            try {
                _state.value = if (reconnectPolicy.attempts == 0) StreamState.CONNECTING
                else StreamState.RECONNECTING

                channel = buildChannel(endpoint.host, endpoint.port, endpoint.useTls)
                runOneSession(channel)
                reconnectPolicy.reset()
            } catch (ce: CancellationException) {
                throw ce
            } catch (t: Throwable) {
                Log.w(TAG, "Stream session failed: ${t.javaClass.simpleName}: ${t.message}")
                _state.value = StreamState.ERROR
            } finally {
                channel?.shutdownNow()
                channel?.awaitTermination(2, TimeUnit.SECONDS)
            }

            if (!scope.isActive) break
            val delayMs = reconnectPolicy.nextDelayMs()
            _metrics.value = _metrics.value.copy(reconnects = _metrics.value.reconnects + 1)
            _state.value = StreamState.RECONNECTING
            Log.i(TAG, "Reconnecting in ${delayMs}ms (attempt=${reconnectPolicy.attempts})")
            delay(delayMs)
        }
    }

    private suspend fun runOneSession(channel: ManagedChannel) {
        val authed = attachAuth(channel)
        val stub = DroneAudioStreamGrpcKt.DroneAudioStreamCoroutineStub(authed)
        val outbound = Channel<ClientStreamMessage>(
            capacity = 32,
            onBufferOverflow = BufferOverflow.DROP_OLDEST
        )

        val handshake = buildHandshake()
        outbound.trySend(handshake)

        val frameForwarder = scope.launch {
            producer.frames.collect { captured ->
                val message = buildAudioMessage(captured)
                val result = outbound.trySend(message)
                if (!result.isSuccess) {
                    _metrics.value = _metrics.value.copy(
                        droppedFrames = _metrics.value.droppedFrames + 1
                    )
                } else {
                    _metrics.value = _metrics.value.copy(
                        framesSent = _metrics.value.framesSent + 1
                    )
                }
            }
        }

        val extrasForwarder = scope.launch {
            for (extra in outboundExtras) {
                outbound.trySend(extra)
            }
        }

        try {
            stub.streamAudio(outbound.receiveAsFlow()).collect { command ->
                if (_state.value != StreamState.STREAMING) {
                    _state.value = StreamState.STREAMING
                }
                handleServerCommand(command)
            }
        } finally {
            frameForwarder.cancel()
            extrasForwarder.cancel()
            outbound.close()
        }
    }

    private fun handleServerCommand(command: ServerCommand) {
        when {
            command.hasSessionAck() -> {
                Log.i(TAG, "Session ack: ${command.sessionAck.assignedSessionId}")
            }
            command.hasFrameAck() -> {
                _metrics.value = _metrics.value.copy(
                    lastAckTimestampMs = System.currentTimeMillis(),
                    lastAckedSequence = command.frameAck.ackedThroughSequence.toLong()
                )
            }
            command.hasConfigUpdate() -> {
                Log.i(TAG, "Config update: ${command.configUpdate.configVersion}")
            }
            command.hasControl() -> {
                Log.i(TAG, "Control command: ${command.control.type}")
                if (command.control.type == ControlType.CONTROL_TYPE_WIPE_DEVICE) {
                    handleWipeCommand(command.commandId)
                }
            }
            else -> {
                Log.d(TAG, "Server command without recognized payload (id=${command.commandId})")
            }
        }
    }

    private fun handleWipeCommand(commandId: String) {
        Log.w(TAG, "Received remote wipe command id=$commandId; invoking WipeHandler")
        try {
            WipeHandler(context).execute(reason = "gateway-command:$commandId")
        } catch (t: Throwable) {
            Log.e(TAG, "WipeHandler.execute failed", t)
        }
    }

    private fun attachAuth(channel: ManagedChannel): io.grpc.Channel {
        val audience = AppConfig.get(context).jwtAudience
        if (audience.isBlank()) return channel
        val jwt = try {
            JwtSigner(DeviceIdentity.get(context)).sign(audience = audience)
        } catch (t: Throwable) {
            Log.w(TAG, "JWT signing failed; sending request unauthenticated: ${t.message}")
            return channel
        }
        val metadata = Metadata().apply {
            put(
                Metadata.Key.of("authorization", Metadata.ASCII_STRING_MARSHALLER),
                "Bearer $jwt",
            )
        }
        val interceptor = MetadataUtils.newAttachHeadersInterceptor(metadata)
        return ClientInterceptors.intercept(channel, interceptor)
    }

    private fun buildChannel(host: String, port: Int, useTls: Boolean): ManagedChannel {
        val builder = OkHttpChannelBuilder.forAddress(host, port)
            .keepAliveTime(30, TimeUnit.SECONDS)
            .keepAliveTimeout(10, TimeUnit.SECONDS)
            .keepAliveWithoutCalls(true)
            .userAgent("DroneSensor/${BuildConfig.VERSION_NAME}")
        return if (useTls) builder.useTransportSecurity().build()
        else builder.usePlaintext().build()
    }

    private fun buildHandshake(): ClientStreamMessage {
        val config = AppConfig.get(context)
        val deviceId = DeviceIdentity.get(context).deviceId
        val health = DeviceHealthSnapshot.capture(
            context = context,
            droppedFrames = producer.droppedFrames.value,
            reconnectCount = _metrics.value.reconnects,
            queueDepth = 0,
            microphoneActive = producer.isActive.value
        )
        val builder = ConnectHandshake.newBuilder()
            .setDeviceId(deviceId)
            .setConnectTimestampMs(System.currentTimeMillis())
            .setAppVersion(BuildConfig.VERSION_NAME)
            .setDeviceModel("${Build.MANUFACTURER} ${Build.MODEL}")
            .setOsVersion("Android ${Build.VERSION.RELEASE} (sdk ${Build.VERSION.SDK_INT})")
            .setAssignedSiteLabel(config.siteLabel)
            .setHealth(health)
            .setSampleRateHz(config.sampleRateHz)
            .setFrameDurationMs(config.frameDurationMs)
        locationProvider?.state?.value?.let { loc ->
            builder.location = loc
        }
        return ClientStreamMessage.newBuilder().setHandshake(builder.build()).build()
    }

    private fun buildAudioMessage(frame: CapturedFrame): ClientStreamMessage {
        val deviceId = DeviceIdentity.get(context).deviceId
        val audio = AudioFrame.newBuilder()
            .setDeviceId(deviceId)
            .setCaptureTimestampMs(frame.captureTimestampMs)
            .setSequenceNumber(frame.sequenceNumber)
            .setSampleRateHz(frame.sampleRateHz)
            .setPcm16Mono(ByteString.copyFrom(frame.pcm16Mono))
            .build()
        return ClientStreamMessage.newBuilder().setAudioFrame(audio).build()
    }

    companion object {
        private const val TAG = "StreamingClient"
    }
}
