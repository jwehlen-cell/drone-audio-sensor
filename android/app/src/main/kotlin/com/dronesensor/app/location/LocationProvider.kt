package com.dronesensor.app.location

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.util.Log
import androidx.core.content.ContextCompat
import com.dronesensor.proto.DeviceLocation
import com.dronesensor.proto.LocationStatus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Lightweight location source backed by android.location.LocationManager
 * (no Play Services dependency, which matters for dedicated devices that
 * may ship without Google services). Combines GPS + network providers and
 * exposes the freshest reading via a StateFlow.
 */
class LocationProvider(private val context: Context) {

    private val lm: LocationManager? =
        ContextCompat.getSystemService(context, LocationManager::class.java)

    private val _state = MutableStateFlow<DeviceLocation?>(null)
    val state: StateFlow<DeviceLocation?> = _state.asStateFlow()

    private var registered = false

    private val listener = object : LocationListener {
        override fun onLocationChanged(location: Location) {
            _state.value = location.toProto(LocationStatus.LOCATION_STATUS_CURRENT)
        }

        @Deprecated("not used on API 26+")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) {}
    }

    fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED ||
                ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")
    fun start(minIntervalMs: Long = 30_000, minDistanceM: Float = 25f) {
        val manager = lm ?: return
        if (!hasPermission()) return
        if (registered) return

        bootstrapLastKnown(manager)

        runCatching {
            if (manager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                manager.requestLocationUpdates(
                    LocationManager.GPS_PROVIDER,
                    minIntervalMs,
                    minDistanceM,
                    listener,
                )
            }
        }.onFailure { Log.w(TAG, "GPS provider request failed: ${it.message}") }

        runCatching {
            if (manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                manager.requestLocationUpdates(
                    LocationManager.NETWORK_PROVIDER,
                    minIntervalMs,
                    minDistanceM,
                    listener,
                )
            }
        }.onFailure { Log.w(TAG, "Network provider request failed: ${it.message}") }

        registered = true
    }

    @SuppressLint("MissingPermission")
    private fun bootstrapLastKnown(manager: LocationManager) {
        val best = sequenceOf(
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER,
        )
            .mapNotNull { runCatching { manager.getLastKnownLocation(it) }.getOrNull() }
            .maxByOrNull { it.time }

        if (best != null) {
            _state.value = best.toProto(LocationStatus.LOCATION_STATUS_LAST_KNOWN)
        } else {
            _state.value = DeviceLocation.newBuilder()
                .setStatus(LocationStatus.LOCATION_STATUS_UNAVAILABLE)
                .build()
        }
    }

    fun stop() {
        if (!registered) return
        lm?.removeUpdates(listener)
        registered = false
    }

    companion object {
        private const val TAG = "LocationProvider"
    }
}

private fun Location.toProto(status: LocationStatus): DeviceLocation {
    val builder = DeviceLocation.newBuilder()
        .setLatitude(latitude)
        .setLongitude(longitude)
        .setLocationTimestampMs(time)
        .setProvider(provider ?: "unknown")
        .setStatus(status)
    if (hasAltitude()) builder.altitudeMeters = altitude
    if (hasAccuracy()) builder.horizontalAccuracyMeters = accuracy
    return builder.build()
}
