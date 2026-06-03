package com.dronesensor.app

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.View
import android.widget.ArrayAdapter
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.dronesensor.app.admin.KioskController
import com.dronesensor.app.audio.AudioCaptureService
import com.dronesensor.app.audio.ServiceStatus
import com.dronesensor.app.config.AppConfig
import com.dronesensor.app.databinding.ActivityMainBinding
import com.dronesensor.app.identity.DeviceIdentity
import com.dronesensor.app.setup.ConnectionPhase
import com.dronesensor.app.setup.WifiCandidate
import com.dronesensor.app.setup.WifiSetupHelper
import com.dronesensor.app.stream.StreamMetrics
import com.dronesensor.app.stream.StreamState
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var kiosk: KioskController
    private lateinit var wifiSetup: WifiSetupHelper

    private val requestPermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ ->
        startServiceIfPermitted()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.valueDevice.text = DeviceIdentity.get(this).deviceId
        val endpoint = AppConfig.get(this).endpoint
        val scheme = if (endpoint.useTls) "grpcs" else "grpc"
        binding.valueEndpoint.text = "$scheme://${endpoint.host}:${endpoint.port}"

        // Version: "Gen 2.${VERSION_CODE}". versionCode is bumped on
        // each release so the operator can confirm at a glance which
        // build is on the phone.
        binding.valueVersion.text = "Gen 2.${BuildConfig.VERSION_CODE}"

        binding.buttonStart.setOnClickListener { ensurePermissionsThenStart() }
        binding.buttonStop.setOnClickListener { AudioCaptureService.stop(this) }

        kiosk = KioskController(this)
        // Apply kiosk *policies* (allowlist self, persistent HOME) on create —
        // but DO NOT call startLockTask() yet. Lock task waits for first
        // cloud check-in so the installer can still use Wi-Fi setup.
        kiosk.applyKioskPolicies()

        wifiSetup = WifiSetupHelper(this)
        binding.setupScanButton.setOnClickListener { wifiSetup.scan() }
        binding.setupJoinButton.setOnClickListener { onJoinClicked() }

        observeServiceStatus()
        observeSetupSignals()
        observeUptime()
    }

    /**
     * Repaint Up since + Total uptime once per second. Boot wall-
     * clock is derived as ``System.currentTimeMillis() -
     * SystemClock.elapsedRealtime()`` (the latter ticks across deep
     * sleep). The ticker is gated on STARTED so it pauses when the
     * activity goes off-screen.
     */
    private fun observeUptime() {
        lifecycleScope.launch {
            repeatOnLifecycle(androidx.lifecycle.Lifecycle.State.STARTED) {
                val upSinceFmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
                while (true) {
                    val elapsedMs = SystemClock.elapsedRealtime()
                    val bootMs = System.currentTimeMillis() - elapsedMs
                    binding.valueUpSince.text = upSinceFmt.format(Date(bootMs))
                    binding.valueUptime.text = formatUptime(elapsedMs)
                    delay(1_000)
                }
            }
        }
    }

    private fun formatUptime(elapsedMs: Long): String {
        val totalSec = elapsedMs / 1000
        val days = totalSec / 86_400
        val hours = (totalSec % 86_400) / 3600
        val minutes = (totalSec % 3600) / 60
        val seconds = totalSec % 60
        return when {
            days > 0 -> String.format(Locale.US, "%dd %dh %dm %ds", days, hours, minutes, seconds)
            hours > 0 -> String.format(Locale.US, "%dh %dm %ds", hours, minutes, seconds)
            else -> String.format(Locale.US, "%dm %ds", minutes, seconds)
        }
    }

    override fun onResume() {
        super.onResume()
        if (!ServiceStatus.running.value) {
            ensurePermissionsThenStart()
        }
        renderViewMode()
    }

    /**
     * Toggle between the setup container (pre-first-checkin) and the
     * status container (post-first-checkin). Also gates kiosk lock task.
     */
    private fun renderViewMode() {
        val setupComplete = AppConfig.get(this).setupComplete
        if (setupComplete) {
            binding.setupContainer.visibility = View.GONE
            binding.statusContainer.visibility = View.VISIBLE
            kiosk.enterLockTask(this)
        } else {
            binding.setupContainer.visibility = View.VISIBLE
            binding.statusContainer.visibility = View.GONE
            // Best-effort: if we were previously in lock task, exit so
            // the installer can interact with the system Wi-Fi picker
            // / our setup UI.
            kiosk.exitLockTask(this)
        }
    }

    private fun observeServiceStatus() {
        lifecycleScope.launch {
            repeatOnLifecycle(androidx.lifecycle.Lifecycle.State.STARTED) {
                combine(ServiceStatus.state, ServiceStatus.metrics) { state, metrics ->
                    state to metrics
                }.collect { (state, metrics) ->
                    renderStatus(state, metrics)
                }
            }
        }
    }

    private fun observeSetupSignals() {
        lifecycleScope.launch {
            repeatOnLifecycle(androidx.lifecycle.Lifecycle.State.STARTED) {
                ServiceStatus.connectionPhase.collect { phase ->
                    renderSetupPhase(phase)
                    if (phase == ConnectionPhase.CLOUD_AUTHENTICATED) {
                        // setupComplete is already flipped in StreamingClient
                        // on first SessionAck; this just makes the UI react.
                        renderViewMode()
                    }
                }
            }
        }
        lifecycleScope.launch {
            repeatOnLifecycle(androidx.lifecycle.Lifecycle.State.STARTED) {
                wifiSetup.scanResults.collect { renderWifiList(it) }
            }
        }
        lifecycleScope.launch {
            repeatOnLifecycle(androidx.lifecycle.Lifecycle.State.STARTED) {
                wifiSetup.lastError.collect { msg ->
                    if (!msg.isNullOrBlank()) {
                        binding.setupResultLabel.text = getString(R.string.setup_failed, msg)
                    }
                }
            }
        }
    }

    private fun renderStatus(state: StreamState, metrics: StreamMetrics) {
        val (label, color) = when (state) {
            StreamState.IDLE -> getString(R.string.state_idle) to Color.GRAY
            StreamState.CONNECTING -> getString(R.string.state_connecting) to Color.YELLOW
            StreamState.STREAMING -> getString(R.string.state_streaming) to Color.GREEN
            StreamState.RECONNECTING -> getString(R.string.state_reconnecting) to Color.YELLOW
            StreamState.ERROR -> getString(R.string.state_error) to Color.RED
        }
        binding.headerStatus.text = label
        binding.headerStatus.setTextColor(color)
        binding.statusDot.setBackgroundColor(color)

        binding.valueFramesSent.text = metrics.framesSent.toString()
        binding.valueDropped.text = metrics.droppedFrames.toString()
        binding.valueReconnects.text = metrics.reconnects.toString()
        binding.valueLastAck.text = if (metrics.lastAckTimestampMs == 0L) {
            getString(R.string.last_ack_never)
        } else {
            val ageSec = (System.currentTimeMillis() - metrics.lastAckTimestampMs) / 1000
            "${ageSec}s ago (seq ${metrics.lastAckedSequence})"
        }
    }

    private fun renderSetupPhase(phase: ConnectionPhase) {
        binding.setupPhaseLabel.text = phase.installerLabel
        val color = when (phase) {
            ConnectionPhase.CLOUD_AUTHENTICATED -> Color.parseColor("#2E7D32")
            ConnectionPhase.CLOUD_REACHABLE,
            ConnectionPhase.WIFI_CONNECTED,
            ConnectionPhase.CELLULAR_AVAILABLE -> Color.parseColor("#F9A825")
            ConnectionPhase.WIFI_AVAILABLE,
            ConnectionPhase.SEARCHING_CELLULAR -> Color.parseColor("#F9A825")
            ConnectionPhase.NO_NETWORK,
            ConnectionPhase.NO_SIM_NO_WIFI -> Color.parseColor("#C62828")
        }
        binding.setupPhaseDot.setBackgroundColor(color)

        val cellularLabel = when {
            phase == ConnectionPhase.NO_SIM_NO_WIFI -> getString(R.string.setup_no_sim)
            phase == ConnectionPhase.SEARCHING_CELLULAR ->
                "Cellular: SIM present, searching for signal"
            phase == ConnectionPhase.CELLULAR_AVAILABLE ->
                "Cellular: signal acquired"
            else -> "Cellular: standing by"
        }
        binding.setupCellularLabel.text = cellularLabel

        if (phase == ConnectionPhase.CLOUD_AUTHENTICATED) {
            binding.setupResultLabel.text = getString(R.string.setup_signed_in)
        }
    }

    private fun renderWifiList(candidates: List<WifiCandidate>) {
        if (candidates.isEmpty()) {
            binding.setupSsidSpinner.adapter = ArrayAdapter(
                this,
                android.R.layout.simple_spinner_dropdown_item,
                arrayOf(getString(R.string.setup_searching)),
            )
            return
        }
        val labels = candidates.map { c ->
            val secure = if (c.secured) "" else " (open)"
            "${c.ssid}  [${c.rssi} dBm]$secure"
        }
        val adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            labels,
        )
        binding.setupSsidSpinner.adapter = adapter
        binding.setupSsidSpinner.tag = candidates
    }

    @Suppress("UNCHECKED_CAST")
    private fun onJoinClicked() {
        val tag = binding.setupSsidSpinner.tag as? List<WifiCandidate>
        val pos = binding.setupSsidSpinner.selectedItemPosition
        val candidate = tag?.getOrNull(pos) ?: return
        val pwd = binding.setupPasswordInput.text?.toString()
        if (candidate.secured && pwd.isNullOrEmpty()) {
            binding.setupResultLabel.text = getString(R.string.setup_failed, "password required")
            return
        }
        binding.setupResultLabel.text = getString(R.string.setup_attempting, candidate.ssid)
        wifiSetup.suggest(candidate.ssid, pwd?.takeIf { candidate.secured })
    }

    private fun ensurePermissionsThenStart() {
        val needed = buildList {
            if (!hasPermission(Manifest.permission.RECORD_AUDIO)) add(Manifest.permission.RECORD_AUDIO)
            if (!hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) {
                add(Manifest.permission.ACCESS_FINE_LOCATION)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                !hasPermission(Manifest.permission.POST_NOTIFICATIONS)
            ) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isEmpty()) {
            startServiceIfPermitted()
        } else {
            requestPermissions.launch(needed.toTypedArray())
        }
    }

    private fun startServiceIfPermitted() {
        if (!hasPermission(Manifest.permission.RECORD_AUDIO)) return
        AudioCaptureService.start(this)
    }

    private fun hasPermission(name: String): Boolean =
        ContextCompat.checkSelfPermission(this, name) == PackageManager.PERMISSION_GRANTED
}
