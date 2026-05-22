package com.dronesensor.app.setup

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.content.ContextCompat
import com.dronesensor.app.stream.StreamState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * Joins three signals into a single ConnectionPhase:
 *  - Android ConnectivityManager network state
 *  - Whether a SIM is present at all (TelephonyManager)
 *  - The streaming client's own session state (StreamState)
 *
 * Independent of which Activity/Fragment is on screen — owned by
 * AudioCaptureService for as long as the service runs.
 */
class ConnectivityMonitor(
    private val context: Context,
    private val streamState: StateFlow<StreamState>,
    private val firstAuthSeen: StateFlow<Boolean>,
) {
    private val cm = ContextCompat.getSystemService(context, ConnectivityManager::class.java)
    private val tm = ContextCompat.getSystemService(context, TelephonyManager::class.java)

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var callback: ConnectivityManager.NetworkCallback? = null

    private val _cellularUp = MutableStateFlow(false)
    private val _wifiUp = MutableStateFlow(false)
    private val _internetValidated = MutableStateFlow(false)

    private val _phase = MutableStateFlow(ConnectionPhase.NO_NETWORK)
    val phase: StateFlow<ConnectionPhase> = _phase.asStateFlow()

    val cellularAvailable: StateFlow<Boolean> = _cellularUp.asStateFlow()
    val wifiConnected: StateFlow<Boolean> = _wifiUp.asStateFlow()

    fun start() {
        if (callback != null) return
        val mgr = cm ?: return

        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = updateFromCaps(mgr.getNetworkCapabilities(network))
            override fun onLost(network: Network) = recomputeAllNetworks()
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                updateFromCaps(caps)
            }
        }

        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        mgr.registerNetworkCallback(request, cb)
        callback = cb

        // Seed initial state.
        recomputeAllNetworks()

        // Compose all signals into the public ConnectionPhase StateFlow.
        job = scope.launch {
            combine(
                _cellularUp,
                _wifiUp,
                _internetValidated,
                streamState,
                firstAuthSeen,
            ) { cellular, wifi, validated, stream, authed ->
                phaseFor(cellular, wifi, validated, stream, authed, hasSim())
            }.collect { _phase.value = it }
        }
    }

    fun stop() {
        val mgr = cm
        callback?.let { cb -> mgr?.runCatching { unregisterNetworkCallback(cb) } }
        callback = null
        job?.cancel()
        job = null
    }

    fun hasSim(): Boolean {
        val mgr = tm ?: return false
        return when (mgr.simState) {
            TelephonyManager.SIM_STATE_ABSENT,
            TelephonyManager.SIM_STATE_UNKNOWN -> false
            else -> true
        }
    }

    private fun recomputeAllNetworks() {
        val mgr = cm ?: return
        var cellular = false
        var wifi = false
        var validated = false
        for (network in mgr.allNetworks) {
            val caps = mgr.getNetworkCapabilities(network) ?: continue
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) cellular = true
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) wifi = true
            if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) validated = true
        }
        _cellularUp.value = cellular
        _wifiUp.value = wifi
        _internetValidated.value = validated
    }

    private fun updateFromCaps(caps: NetworkCapabilities?) {
        if (caps == null) return
        val mgr = cm ?: return
        // It's easier to recompute the whole picture than to track a per-
        // network map.
        recomputeAllNetworks()
        Log.d(TAG, "net caps: cellular=${_cellularUp.value} wifi=${_wifiUp.value} validated=${_internetValidated.value}")
    }

    private fun phaseFor(
        cellular: Boolean,
        wifi: Boolean,
        validated: Boolean,
        stream: StreamState,
        firstAuth: Boolean,
        sim: Boolean,
    ): ConnectionPhase {
        if (firstAuth || stream == StreamState.STREAMING) return ConnectionPhase.CLOUD_AUTHENTICATED
        if (stream == StreamState.CONNECTING || stream == StreamState.RECONNECTING) {
            return ConnectionPhase.CLOUD_REACHABLE
        }
        if (wifi && validated) return ConnectionPhase.WIFI_CONNECTED
        if (wifi) return ConnectionPhase.WIFI_AVAILABLE
        if (cellular && validated) return ConnectionPhase.CELLULAR_AVAILABLE
        if (cellular) return ConnectionPhase.SEARCHING_CELLULAR
        if (!sim) return ConnectionPhase.NO_SIM_NO_WIFI
        return ConnectionPhase.NO_NETWORK
    }

    companion object {
        private const val TAG = "ConnectivityMonitor"
    }
}
