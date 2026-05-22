package com.dronesensor.app.config

import android.content.Context
import androidx.core.content.edit
import com.dronesensor.app.BuildConfig

data class StreamEndpoint(
    val host: String,
    val port: Int,
    val useTls: Boolean
)

class AppConfig private constructor(context: Context) {

    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    val sampleRateHz: Int = 16_000
    val frameDurationMs: Int = 1_000
    val maxLocalQueueFrames: Int = 10

    var endpoint: StreamEndpoint
        get() = StreamEndpoint(
            host = prefs.getString(KEY_HOST, BuildConfig.DEFAULT_GRPC_HOST)!!,
            port = prefs.getInt(KEY_PORT, BuildConfig.DEFAULT_GRPC_PORT),
            useTls = prefs.getBoolean(KEY_TLS, BuildConfig.DEFAULT_TLS)
        )
        set(value) = prefs.edit {
            putString(KEY_HOST, value.host)
            putInt(KEY_PORT, value.port)
            putBoolean(KEY_TLS, value.useTls)
        }

    var siteLabel: String
        get() = prefs.getString(KEY_SITE_LABEL, "")!!
        set(value) = prefs.edit { putString(KEY_SITE_LABEL, value) }

    /**
     * JWT audience claim, normally the gateway URL. Leaving this blank
     * disables JWT auth on the client side (use only for local dev /
     * pre-provisioning testing).
     */
    var jwtAudience: String
        get() = prefs.getString(KEY_JWT_AUDIENCE, "")!!
        set(value) = prefs.edit { putString(KEY_JWT_AUDIENCE, value) }

    /**
     * True after the device has completed its first successful cloud
     * check-in. The Wi-Fi setup UI / unrestricted-kiosk window is only
     * shown when this is false.
     */
    var setupComplete: Boolean
        get() = prefs.getBoolean(KEY_SETUP_COMPLETE, false)
        set(value) = prefs.edit { putBoolean(KEY_SETUP_COMPLETE, value) }

    /** Unix ms of the most recent connectivity-watchdog reboot. */
    var lastRebootMs: Long
        get() = prefs.getLong(KEY_LAST_REBOOT_MS, 0L)
        set(value) = prefs.edit { putLong(KEY_LAST_REBOOT_MS, value) }

    /** Short tag describing why the watchdog rebooted last. */
    var lastRebootReason: String
        get() = prefs.getString(KEY_LAST_REBOOT_REASON, "")!!
        set(value) = prefs.edit { putString(KEY_LAST_REBOOT_REASON, value) }

    /** Minimum delay between watchdog-driven reboots. */
    val minRebootIntervalMs: Long = 10 * 60 * 1000

    /** Connectivity loss threshold that triggers a reboot. */
    val connectivityRebootTimeoutMs: Long = 5 * 60 * 1000

    /** Setup-phase timeout before the installer-facing Wi-Fi UI appears. */
    val setupCellularGraceMs: Long = 30 * 1000

    companion object {
        private const val PREFS = "drone_sensor_config"
        private const val KEY_HOST = "grpc_host"
        private const val KEY_PORT = "grpc_port"
        private const val KEY_TLS = "grpc_tls"
        private const val KEY_SITE_LABEL = "site_label"
        private const val KEY_JWT_AUDIENCE = "jwt_audience"
        private const val KEY_SETUP_COMPLETE = "setup_complete"
        private const val KEY_LAST_REBOOT_MS = "last_reboot_ms"
        private const val KEY_LAST_REBOOT_REASON = "last_reboot_reason"

        @Volatile private var instance: AppConfig? = null

        fun get(context: Context): AppConfig =
            instance ?: synchronized(this) {
                instance ?: AppConfig(context).also { instance = it }
            }
    }
}
