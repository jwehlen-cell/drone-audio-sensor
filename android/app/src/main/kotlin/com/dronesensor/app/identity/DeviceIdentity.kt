package com.dronesensor.app.identity

import android.content.Context
import androidx.core.content.edit
import java.util.UUID

class DeviceIdentity private constructor(context: Context) {

    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    val deviceId: String by lazy {
        prefs.getString(KEY_DEVICE_ID, null) ?: generateAndPersist()
    }

    fun overrideDeviceId(newId: String) {
        prefs.edit { putString(KEY_DEVICE_ID, newId) }
    }

    private fun generateAndPersist(): String {
        val id = "DRONE-SENSOR-" + UUID.randomUUID().toString().take(8).uppercase()
        prefs.edit { putString(KEY_DEVICE_ID, id) }
        return id
    }

    companion object {
        private const val PREFS = "drone_sensor_identity"
        private const val KEY_DEVICE_ID = "device_id"

        @Volatile private var instance: DeviceIdentity? = null

        fun get(context: Context): DeviceIdentity =
            instance ?: synchronized(this) {
                instance ?: DeviceIdentity(context).also { instance = it }
            }
    }
}
