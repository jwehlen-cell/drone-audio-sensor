package com.dronesensor.app

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
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
import com.dronesensor.app.stream.StreamMetrics
import com.dronesensor.app.stream.StreamState
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var kiosk: KioskController

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

        binding.buttonStart.setOnClickListener { ensurePermissionsThenStart() }
        binding.buttonStop.setOnClickListener { AudioCaptureService.stop(this) }

        kiosk = KioskController(this)
        kiosk.applyKioskPolicies()

        observeStatus()
    }

    override fun onResume() {
        super.onResume()
        if (!ServiceStatus.running.value) {
            ensurePermissionsThenStart()
        }
        kiosk.enterLockTask(this)
    }

    private fun observeStatus() {
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
