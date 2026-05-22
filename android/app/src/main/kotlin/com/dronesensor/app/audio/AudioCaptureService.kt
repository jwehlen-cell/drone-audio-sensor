package com.dronesensor.app.audio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.dronesensor.app.MainActivity
import com.dronesensor.app.R
import com.dronesensor.app.config.AppConfig
import com.dronesensor.app.health.HealthReporter
import com.dronesensor.app.location.LocationProvider
import com.dronesensor.app.setup.ConnectionPhase
import com.dronesensor.app.setup.ConnectivityMonitor
import com.dronesensor.app.setup.ConnectivityWatchdog
import com.dronesensor.app.stream.StreamMetrics
import com.dronesensor.app.stream.StreamState
import com.dronesensor.app.stream.StreamingClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.launch

class AudioCaptureService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var producer: AudioFrameProducer? = null
    private var client: StreamingClient? = null
    private var locationProvider: LocationProvider? = null
    private var watchdog: AudioWatchdog? = null
    private var healthReporter: HealthReporter? = null
    private var connectivityMonitor: ConnectivityMonitor? = null
    private var connectivityWatchdog: ConnectivityWatchdog? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var observerJob: Job? = null
    private var locationObserverJob: Job? = null
    private var phaseObserverJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        ensureNotificationChannel()
        acquireWakeLock()
        ServiceStatus.setRunning(true)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground()

        if (producer == null) {
            val config = AppConfig.get(this)
            val loc = LocationProvider(this)
            val p = AudioFrameProducer(
                context = this,
                sampleRateHz = config.sampleRateHz,
                frameDurationMs = config.frameDurationMs,
                queueCapacity = config.maxLocalQueueFrames
            )
            val c = StreamingClient(this, p, loc)
            val wd = AudioWatchdog(p)
            val hr = HealthReporter(this, p, c, wd)

            producer = p
            client = c
            locationProvider = loc
            watchdog = wd
            healthReporter = hr

            val cm = ConnectivityMonitor(
                context = this,
                streamState = c.state,
                firstAuthSeen = c.firstAuthSeen,
            )
            val cwd = ConnectivityWatchdog(this, cm.phase)
            connectivityMonitor = cm
            connectivityWatchdog = cwd

            loc.start()
            p.start()
            c.start()
            wd.start()
            hr.start()
            cm.start()
            cwd.start()

            observerJob = scope.launch {
                combine(c.state, c.metrics) { state, metrics ->
                    state to metrics
                }.collect { (state, metrics) ->
                    ServiceStatus.update(state, metrics)
                }
            }

            phaseObserverJob = scope.launch {
                cm.phase.collect { ServiceStatus.updatePhase(it) }
            }

            locationObserverJob = scope.launch {
                loc.state
                    .filterNotNull()
                    .drop(1)
                    .collect { c.submitLocationUpdate(it) }
            }
        }

        return START_STICKY
    }

    override fun onDestroy() {
        observerJob?.cancel()
        phaseObserverJob?.cancel()
        locationObserverJob?.cancel()
        connectivityWatchdog?.stop()
        connectivityMonitor?.stop()
        healthReporter?.stop()
        watchdog?.stop()
        client?.stop()
        producer?.stop()
        locationProvider?.stop()
        connectivityWatchdog = null
        connectivityMonitor = null
        healthReporter = null
        watchdog = null
        producer = null
        client = null
        locationProvider = null
        releaseWakeLock()
        ServiceStatus.setRunning(false)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startInForeground() {
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val type = if (hasLocationPermission()) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
            } else {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            ServiceCompat.startForeground(this, NOTIF_ID, notification, type)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, android.Manifest.permission.ACCESS_FINE_LOCATION) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED ||
                ContextCompat.checkSelfPermission(this, android.Manifest.permission.ACCESS_COARSE_LOCATION) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED

    private fun buildNotification(): Notification {
        val openIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(getString(R.string.notification_text))
            .setOngoing(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setContentIntent(openIntent)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ContextCompat.getSystemService(this, NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_name),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.notification_channel_description)
            setShowBadge(false)
            enableLights(false)
            enableVibration(false)
        }
        nm.createNotificationChannel(channel)
    }

    private fun acquireWakeLock() {
        val pm = ContextCompat.getSystemService(this, PowerManager::class.java) ?: return
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKELOCK_TAG).also {
            it.setReferenceCounted(false)
            it.acquire()
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.runCatching { if (isHeld) release() }
        wakeLock = null
    }

    companion object {
        private const val TAG = "AudioCaptureService"
        private const val NOTIF_ID = 1001
        private const val CHANNEL_ID = "drone_sensor_monitoring"
        private const val WAKELOCK_TAG = "drone-sensor:capture"

        fun start(context: Context) {
            val intent = Intent(context, AudioCaptureService::class.java)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, AudioCaptureService::class.java)
            context.stopService(intent)
        }
    }
}

object ServiceStatus {
    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running.asStateFlow()

    private val _state = MutableStateFlow(StreamState.IDLE)
    val state: StateFlow<StreamState> = _state.asStateFlow()

    private val _metrics = MutableStateFlow(StreamMetrics())
    val metrics: StateFlow<StreamMetrics> = _metrics.asStateFlow()

    private val _connectionPhase = MutableStateFlow(ConnectionPhase.NO_NETWORK)
    val connectionPhase: StateFlow<ConnectionPhase> = _connectionPhase.asStateFlow()

    internal fun setRunning(v: Boolean) {
        _running.value = v
        if (!v) {
            _state.value = StreamState.IDLE
        }
    }

    internal fun update(state: StreamState, metrics: StreamMetrics) {
        _state.value = state
        _metrics.value = metrics
    }

    internal fun updatePhase(phase: ConnectionPhase) {
        _connectionPhase.value = phase
    }
}
