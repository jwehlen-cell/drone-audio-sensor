package com.dronesensor.app.health

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import androidx.core.content.ContextCompat
import com.dronesensor.app.BuildConfig
import com.dronesensor.proto.DeviceHealth
import com.dronesensor.proto.NetworkType
import com.dronesensor.proto.ThermalState

object DeviceHealthSnapshot {

    fun capture(
        context: Context,
        droppedFrames: Int,
        reconnectCount: Int,
        queueDepth: Int,
        microphoneActive: Boolean
    ): DeviceHealth {
        val battery = batteryStatus(context)
        return DeviceHealth.newBuilder()
            .setBatteryPercent(battery.percent)
            .setCharging(battery.charging)
            .setNetworkType(networkType(context))
            .setDroppedFrames(droppedFrames)
            .setReconnectCount(reconnectCount)
            .setAppVersion(BuildConfig.VERSION_NAME)
            .setThermalState(thermalState(context))
            .setQueueDepth(queueDepth)
            .setMicrophoneActive(microphoneActive)
            .build()
    }

    private data class Battery(val percent: Int, val charging: Boolean)

    private fun batteryStatus(context: Context): Battery {
        val intent: Intent? = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        if (intent == null) return Battery(-1, false)
        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val percent = if (level >= 0 && scale > 0) (level * 100) / scale else -1
        val status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
        return Battery(percent, charging)
    }

    private fun networkType(context: Context): NetworkType {
        val cm = ContextCompat.getSystemService(context, ConnectivityManager::class.java)
            ?: return NetworkType.NETWORK_TYPE_UNSPECIFIED
        val active = cm.activeNetwork ?: return NetworkType.NETWORK_TYPE_NONE
        val caps = cm.getNetworkCapabilities(active) ?: return NetworkType.NETWORK_TYPE_NONE
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> NetworkType.NETWORK_TYPE_WIFI
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> NetworkType.NETWORK_TYPE_ETHERNET
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> NetworkType.NETWORK_TYPE_CELLULAR_OTHER
            else -> NetworkType.NETWORK_TYPE_UNSPECIFIED
        }
    }

    private fun thermalState(context: Context): ThermalState {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return ThermalState.THERMAL_STATE_UNSPECIFIED
        val pm = ContextCompat.getSystemService(context, PowerManager::class.java)
            ?: return ThermalState.THERMAL_STATE_UNSPECIFIED
        return when (pm.currentThermalStatus) {
            PowerManager.THERMAL_STATUS_NONE -> ThermalState.THERMAL_STATE_NOMINAL
            PowerManager.THERMAL_STATUS_LIGHT -> ThermalState.THERMAL_STATE_LIGHT
            PowerManager.THERMAL_STATUS_MODERATE -> ThermalState.THERMAL_STATE_MODERATE
            PowerManager.THERMAL_STATUS_SEVERE -> ThermalState.THERMAL_STATE_SEVERE
            PowerManager.THERMAL_STATUS_CRITICAL -> ThermalState.THERMAL_STATE_CRITICAL
            PowerManager.THERMAL_STATUS_EMERGENCY -> ThermalState.THERMAL_STATE_EMERGENCY
            else -> ThermalState.THERMAL_STATE_UNSPECIFIED
        }
    }
}
