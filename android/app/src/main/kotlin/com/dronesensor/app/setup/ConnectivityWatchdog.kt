package com.dronesensor.app.setup

import android.app.admin.DevicePolicyManager
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat
import com.dronesensor.app.admin.DeviceOwnerReceiver
import com.dronesensor.app.audio.AudioCaptureService
import com.dronesensor.app.config.AppConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Watchdog that reboots the phone if cloud connectivity is missing for
 * more than `AppConfig.connectivityRebootTimeoutMs`.
 *
 * Inputs:
 *  - `phaseFlow` — current ConnectionPhase from ConnectivityMonitor
 *  - the time of the last observed cloud authentication
 *
 * The timer is reset every time the phase reaches CLOUD_AUTHENTICATED.
 * When the timeout fires:
 *  - on a Device Owner install, calls `DevicePolicyManager.reboot()`
 *  - otherwise restarts AudioCaptureService as a softer fallback
 *
 * Anti-loop guard:
 *  - persisted `last_reboot_ms` in SharedPreferences
 *  - reboot is skipped if last reboot was within `minRebootIntervalMs`
 */
class ConnectivityWatchdog(
    private val context: Context,
    private val phaseFlow: StateFlow<ConnectionPhase>,
    private val checkIntervalMs: Long = 30_000,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val config = AppConfig.get(context)
    private val dpm: DevicePolicyManager? =
        ContextCompat.getSystemService(context, DevicePolicyManager::class.java)

    private var job: Job? = null
    private var observerJob: Job? = null
    @Volatile private var lastAuthenticatedAtMs: Long = System.currentTimeMillis()

    fun start() {
        if (job?.isActive == true) return
        lastAuthenticatedAtMs = System.currentTimeMillis()

        observerJob = scope.launch {
            phaseFlow.collect { phase ->
                if (phase == ConnectionPhase.CLOUD_AUTHENTICATED) {
                    lastAuthenticatedAtMs = System.currentTimeMillis()
                }
            }
        }

        job = scope.launch {
            while (isActive) {
                delay(checkIntervalMs)
                val nowMs = System.currentTimeMillis()
                val sinceAuthMs = nowMs - lastAuthenticatedAtMs
                if (sinceAuthMs < config.connectivityRebootTimeoutMs) continue

                val sinceLastRebootMs = nowMs - config.lastRebootMs
                if (config.lastRebootMs != 0L && sinceLastRebootMs < config.minRebootIntervalMs) {
                    Log.w(
                        TAG,
                        "Connectivity timeout but last reboot was ${sinceLastRebootMs}ms ago; " +
                                "below minRebootIntervalMs=${config.minRebootIntervalMs}",
                    )
                    continue
                }

                triggerReboot(sinceAuthMs)
            }
        }
    }

    fun stop() {
        job?.cancel()
        observerJob?.cancel()
        job = null
        observerJob = null
    }

    private fun triggerReboot(sinceAuthMs: Long) {
        val reason = "no_cloud_${sinceAuthMs / 1000}s"
        config.lastRebootMs = System.currentTimeMillis()
        config.lastRebootReason = reason

        val mgr = dpm
        val isOwner = mgr?.isDeviceOwnerApp(context.packageName) == true
        if (mgr != null && isOwner) {
            Log.w(TAG, "Connectivity watchdog: DPM reboot ($reason)")
            try {
                mgr.reboot(DeviceOwnerReceiver.componentName(context))
                return
            } catch (t: Throwable) {
                Log.e(TAG, "DPM reboot failed; falling back to service restart", t)
            }
        }

        // Fallback path — least intrusive thing that still nukes our
        // in-memory gRPC + audio state and reattempts everything fresh.
        Log.w(TAG, "Connectivity watchdog: service-restart fallback ($reason; deviceOwner=$isOwner)")
        runCatching {
            context.stopService(Intent(context, AudioCaptureService::class.java))
        }
        AudioCaptureService.start(context.applicationContext)
    }

    companion object {
        private const val TAG = "ConnectivityWatchdog"
    }
}
