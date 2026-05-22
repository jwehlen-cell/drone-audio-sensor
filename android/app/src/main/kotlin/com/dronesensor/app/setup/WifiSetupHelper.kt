package com.dronesensor.app.setup

import android.Manifest
import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.wifi.ScanResult
import android.net.wifi.WifiManager
import android.net.wifi.WifiNetworkSuggestion
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Thin wrapper around WifiManager that gives the installer-facing UI
 * everything it needs for a Starlink-style fallback flow:
 *
 *   1. scan() to populate a list of visible SSIDs
 *   2. suggest(ssid, password) to add a network suggestion that
 *      auto-connects the device
 *
 * Notes on Android API behavior:
 *  - On Android 10+ we use WifiNetworkSuggestion. The system shows a
 *    one-time approval prompt for non-Device-Owner installs and skips
 *    it for Device Owner.
 *  - The legacy WifiManager.addNetwork() / enableNetwork() path is
 *    deprecated on Android 10+ and silently no-ops for most apps. We
 *    do not use it.
 *  - Scan results require ACCESS_FINE_LOCATION at runtime even though
 *    we don't care about location.
 *
 * Threading: this class is safe to call from the main thread; the scan
 * is fire-and-forget and results land in the StateFlow.
 */
class WifiSetupHelper(private val context: Context) {

    private val wifi: WifiManager? =
        context.applicationContext.getSystemService(WifiManager::class.java)

    private val _scanResults = MutableStateFlow<List<WifiCandidate>>(emptyList())
    val scanResults: StateFlow<List<WifiCandidate>> = _scanResults.asStateFlow()

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError.asStateFlow()

    private val _lastAttemptedSsid = MutableStateFlow<String?>(null)
    val lastAttemptedSsid: StateFlow<String?> = _lastAttemptedSsid.asStateFlow()

    private var scanReceiver: BroadcastReceiver? = null

    fun hasScanPermission(): Boolean {
        val pm = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION)
        return pm == PackageManager.PERMISSION_GRANTED
    }

    @SuppressLint("MissingPermission")
    fun scan(): Boolean {
        val mgr = wifi ?: return false
        if (!hasScanPermission()) {
            _lastError.value = "Location permission required for Wi-Fi scan"
            return false
        }
        registerReceiver()
        val started = mgr.startScan()
        if (!started) {
            // Scans are throttled — fall back to whatever the system has.
            _scanResults.value = mgr.scanResults.mapNotNull(::toCandidate).distinctSorted()
        }
        return true
    }

    fun suggest(ssid: String, password: String?): SuggestionResult {
        val mgr = wifi ?: return SuggestionResult.Error("WifiManager unavailable")
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return SuggestionResult.Error(
                "Wi-Fi setup requires Android 10+ (current=${Build.VERSION.SDK_INT})",
            )
        }

        // Clear any existing suggestion for the same SSID so a retry with
        // a corrected password works.
        runCatching {
            mgr.removeNetworkSuggestions(emptyList()) // remove ALL prior suggestions
        }.onFailure { Log.w(TAG, "removeNetworkSuggestions(empty) failed: ${it.message}") }

        val builder = WifiNetworkSuggestion.Builder().setSsid(ssid)
        if (!password.isNullOrEmpty()) {
            builder.setWpa2Passphrase(password)
        } else {
            builder.setIsAppInteractionRequired(false)
        }
        val suggestion = builder.build()
        val status = mgr.addNetworkSuggestions(listOf(suggestion))
        _lastAttemptedSsid.value = ssid

        return if (status == WifiManager.STATUS_NETWORK_SUGGESTIONS_SUCCESS) {
            _lastError.value = null
            SuggestionResult.Submitted
        } else {
            val message = "Wi-Fi suggestion rejected (status=$status)"
            _lastError.value = message
            SuggestionResult.Error(message)
        }
    }

    fun stop() {
        scanReceiver?.let { rx ->
            runCatching { context.unregisterReceiver(rx) }
        }
        scanReceiver = null
    }

    @SuppressLint("MissingPermission")
    private fun registerReceiver() {
        if (scanReceiver != null) return
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, intent: Intent?) {
                val mgr = wifi ?: return
                _scanResults.value = mgr.scanResults.mapNotNull(::toCandidate).distinctSorted()
            }
        }
        ContextCompat.registerReceiver(
            context,
            rx,
            IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        scanReceiver = rx
    }

    private fun toCandidate(sr: ScanResult): WifiCandidate? {
        val ssid = sr.SSID?.takeIf { it.isNotBlank() } ?: return null
        return WifiCandidate(
            ssid = ssid,
            bssid = sr.BSSID,
            rssi = sr.level,
            capabilities = sr.capabilities ?: "",
            secured = sr.capabilities?.contains("WPA") == true ||
                    sr.capabilities?.contains("WEP") == true,
        )
    }

    private fun List<WifiCandidate>.distinctSorted(): List<WifiCandidate> =
        groupBy { it.ssid }
            .map { (_, group) -> group.maxByOrNull { it.rssi } ?: group.first() }
            .sortedByDescending { it.rssi }

    companion object {
        private const val TAG = "WifiSetupHelper"
    }
}

data class WifiCandidate(
    val ssid: String,
    val bssid: String?,
    val rssi: Int,
    val capabilities: String,
    val secured: Boolean,
)

sealed class SuggestionResult {
    data object Submitted : SuggestionResult()
    data class Error(val message: String) : SuggestionResult()
}
