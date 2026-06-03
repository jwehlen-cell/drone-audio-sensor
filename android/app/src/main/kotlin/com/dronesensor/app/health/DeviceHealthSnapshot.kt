package com.dronesensor.app.health

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.TrafficStats
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.os.StatFs
import android.telephony.CellSignalStrength
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat
import com.dronesensor.app.BuildConfig
import com.dronesensor.app.config.AppConfig
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
        val config = AppConfig.get(context)
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
            .setLastRebootTimestampMs(config.lastRebootMs)
            .setLastRebootReason(config.lastRebootReason)
            // Extended SOH (zero = unset; the gateway/admin treat 0 as
            // "not reported" and skip rendering).
            .setBatteryTemperatureDeciC(battery.temperatureDeciC)
            .setBatteryVoltageMv(battery.voltageMv)
            .setBatteryHealth(battery.health)
            .setCellularRssiDbm(cellularRssiDbm(context))
            .setWifiRssiDbm(wifiRssiDbm(context))
            .setFreeDiskBytes(freeDiskBytes())
            .setTxBytesCumulative(txBytesCumulative(context))
            .build()
    }

    private data class Battery(
        val percent: Int,
        val charging: Boolean,
        val temperatureDeciC: Int,
        val voltageMv: Int,
        val health: Int,
    )

    private fun batteryStatus(context: Context): Battery {
        val intent: Intent? = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        if (intent == null) return Battery(-1, false, 0, 0, 0)
        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val percent = if (level >= 0 && scale > 0) (level * 100) / scale else -1
        val status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
        // EXTRA_TEMPERATURE is tenths of a degree C; EXTRA_VOLTAGE is mV.
        // Default 0 reads as "unset" downstream.
        val tempDeciC = intent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0)
        val voltageMv = intent.getIntExtra(BatteryManager.EXTRA_VOLTAGE, 0)
        val health = intent.getIntExtra(BatteryManager.EXTRA_HEALTH, 0)
        return Battery(percent, charging, tempDeciC, voltageMv, health)
    }

    private fun cellularRssiDbm(context: Context): Int {
        // Requires READ_PHONE_STATE on API < 30, accessible silently for
        // Device Owner apps via the system; return 0 (unset) if denied.
        if (ContextCompat.checkSelfPermission(
                context, Manifest.permission.READ_PHONE_STATE
            ) != PackageManager.PERMISSION_GRANTED
        ) return 0
        val tm = ContextCompat.getSystemService(context, TelephonyManager::class.java) ?: return 0
        return runCatching {
            val ss = tm.signalStrength ?: return 0
            // Prefer per-network strength; fall back to the legacy
            // getLevel()/getDbm() if the OS doesn't surface a CellSignalStrength.
            val first = ss.cellSignalStrengths.firstOrNull { it.dbm < 0 } as CellSignalStrength?
            first?.dbm ?: 0
        }.getOrElse { 0 }
    }

    private fun wifiRssiDbm(context: Context): Int {
        return runCatching {
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
                ?: return 0
            val info = wm.connectionInfo ?: return 0
            val rssi = info.rssi
            if (rssi < 0) rssi else 0  // unconnected returns INVALID_RSSI=-127 or 0
        }.getOrElse { 0 }
    }

    private fun freeDiskBytes(): Long = runCatching {
        StatFs(Environment.getDataDirectory().path).availableBytes
    }.getOrElse { 0L }

    private fun txBytesCumulative(context: Context): Long = runCatching {
        val uid = context.applicationInfo.uid
        val bytes = TrafficStats.getUidTxBytes(uid)
        if (bytes == TrafficStats.UNSUPPORTED.toLong()) 0L else bytes
    }.getOrElse { 0L }

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
