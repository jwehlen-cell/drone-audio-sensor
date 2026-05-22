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

    companion object {
        private const val PREFS = "drone_sensor_config"
        private const val KEY_HOST = "grpc_host"
        private const val KEY_PORT = "grpc_port"
        private const val KEY_TLS = "grpc_tls"
        private const val KEY_SITE_LABEL = "site_label"

        @Volatile private var instance: AppConfig? = null

        fun get(context: Context): AppConfig =
            instance ?: synchronized(this) {
                instance ?: AppConfig(context).also { instance = it }
            }
    }
}
