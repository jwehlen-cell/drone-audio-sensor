package com.dronesensor.app.audio

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Periodically checks the capture pipeline. If the producer's isActive flag
 * is false, or if no new frame has arrived within frameStallThresholdMs while
 * the producer claims to be active, the watchdog attempts a restart.
 */
class AudioWatchdog(
    private val producer: AudioFrameProducer,
    private val intervalMs: Long = 15_000,
    private val frameStallThresholdMs: Long = 10_000
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var frameObserverJob: Job? = null
    private var lastFrameAtMs: Long = 0

    private val _restartCount = MutableStateFlow(0)
    val restartCount: StateFlow<Int> = _restartCount.asStateFlow()

    fun start() {
        if (job?.isActive == true) return
        lastFrameAtMs = System.currentTimeMillis()

        frameObserverJob = scope.launch {
            producer.frames.collect {
                lastFrameAtMs = System.currentTimeMillis()
            }
        }

        job = scope.launch {
            while (isActive) {
                delay(intervalMs)
                val now = System.currentTimeMillis()
                val active = producer.isActive.value
                val staleness = now - lastFrameAtMs
                val needsRestart = !active || staleness > frameStallThresholdMs
                if (needsRestart) {
                    Log.w(
                        TAG,
                        "Restarting audio capture (active=$active, staleness=${staleness}ms)"
                    )
                    runCatching { producer.stop() }
                    runCatching { producer.start() }
                    _restartCount.value = _restartCount.value + 1
                    lastFrameAtMs = System.currentTimeMillis()
                }
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
        frameObserverJob?.cancel()
        frameObserverJob = null
    }

    companion object {
        private const val TAG = "AudioWatchdog"
    }
}
