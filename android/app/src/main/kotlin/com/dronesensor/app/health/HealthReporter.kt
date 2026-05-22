package com.dronesensor.app.health

import android.content.Context
import com.dronesensor.app.audio.AudioFrameProducer
import com.dronesensor.app.audio.AudioWatchdog
import com.dronesensor.app.stream.StreamingClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Sends a DeviceHealth message every intervalMs over the streaming client's
 * outbound side-channel. Independent of audio frames so server-side
 * dashboards keep seeing a heartbeat even if capture briefly stalls.
 */
class HealthReporter(
    private val context: Context,
    private val producer: AudioFrameProducer,
    private val client: StreamingClient,
    private val watchdog: AudioWatchdog,
    private val intervalMs: Long = 30_000
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null

    fun start() {
        if (job?.isActive == true) return
        job = scope.launch {
            while (isActive) {
                val health = DeviceHealthSnapshot.capture(
                    context = context,
                    droppedFrames = producer.droppedFrames.value,
                    reconnectCount = client.metrics.value.reconnects,
                    queueDepth = 0,
                    microphoneActive = producer.isActive.value
                )
                client.submitHealth(health)
                delay(intervalMs)
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }
}
