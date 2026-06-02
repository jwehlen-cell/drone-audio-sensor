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

    /**
     * Free-form per-device tag shown in the admin's "Site" column on
     * detection rows (e.g. "Patrick (emulator)"). Distinct from
     * [site] below, which is the grouping key the admin selector uses.
     */
    var siteLabel: String
        get() = prefs.getString(KEY_SITE_LABEL, BuildConfig.DEFAULT_SITE_LABEL)!!
        set(value) = prefs.edit { putString(KEY_SITE_LABEL, value) }

    /**
     * Site grouping key — e.g. "Patrick", "Shaw". The admin UI's top
     * selector chooses one of these and every view filters by it.
     * Today this comes from BuildConfig.DEFAULT_SITE (set via the
     * gradle -Psite=... property at build time); tomorrow a QR scanner
     * will write the same value here. The handshake doesn't carry
     * this field today; the device's Firestore registry doc is
     * pre-populated with the same value so the gateway's merge-on-
     * handshake doesn't lose it.
     */
    var site: String
        get() = prefs.getString(KEY_SITE, BuildConfig.DEFAULT_SITE)!!
        set(value) = prefs.edit { putString(KEY_SITE, value) }

    /**
     * JWT audience claim, normally the gateway URL. Leaving this blank
     * disables JWT auth on the client side (use only for local dev /
     * pre-provisioning testing).
     */
    var jwtAudience: String
        get() = prefs.getString(KEY_JWT_AUDIENCE, BuildConfig.DEFAULT_JWT_AUDIENCE)!!
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

    /**
     * True after the app has applied a build-time ProvisioningPayload at
     * least once. Stops every fresh launch from re-clobbering values
     * the operator may have updated post-install (e.g. via a future
     * QR scan that replaces the build-time defaults).
     */
    var buildProvisioned: Boolean
        get() = prefs.getBoolean(KEY_BUILD_PROVISIONED, false)
        set(value) = prefs.edit { putBoolean(KEY_BUILD_PROVISIONED, value) }

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
        private const val KEY_SITE = "site"
        private const val KEY_JWT_AUDIENCE = "jwt_audience"
        private const val KEY_SETUP_COMPLETE = "setup_complete"
        private const val KEY_LAST_REBOOT_MS = "last_reboot_ms"
        private const val KEY_LAST_REBOOT_REASON = "last_reboot_reason"
        private const val KEY_BUILD_PROVISIONED = "build_provisioned"

        @Volatile private var instance: AppConfig? = null

        fun get(context: Context): AppConfig =
            instance ?: synchronized(this) {
                instance ?: AppConfig(context).also { instance = it }
            }
    }
}
