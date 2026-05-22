package com.dronesensor.app.audio

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class AudioFrameProducer(
    private val context: Context,
    private val sampleRateHz: Int,
    private val frameDurationMs: Int,
    queueCapacity: Int
) {

    private val frameSamples: Int = sampleRateHz * frameDurationMs / 1000
    private val frameBytes: Int = frameSamples * BYTES_PER_SAMPLE

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var captureJob: Job? = null
    private var audioRecord: AudioRecord? = null

    private val _frames = MutableSharedFlow<CapturedFrame>(
        replay = 0,
        extraBufferCapacity = queueCapacity,
        onBufferOverflow = BufferOverflow.DROP_OLDEST
    )
    val frames: SharedFlow<CapturedFrame> = _frames.asSharedFlow()

    private val _isActive = MutableStateFlow(false)
    val isActive: StateFlow<Boolean> = _isActive.asStateFlow()

    private val _droppedFrames = MutableStateFlow(0)
    val droppedFrames: StateFlow<Int> = _droppedFrames.asStateFlow()

    @SuppressLint("MissingPermission")
    fun start() {
        if (captureJob?.isActive == true) return
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            Log.w(TAG, "RECORD_AUDIO permission not granted; cannot start capture")
            return
        }

        val minBufferBytes = AudioRecord.getMinBufferSize(
            sampleRateHz,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBufferBytes <= 0) {
            Log.e(TAG, "Invalid AudioRecord min buffer size: $minBufferBytes")
            return
        }

        // Allocate at least 2 seconds of buffer so we don't overrun while we build 1-sec frames.
        val recordBufferBytes = maxOf(minBufferBytes * 2, frameBytes * 2)

        val record = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            sampleRateHz,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            recordBufferBytes
        )

        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord failed to initialize (state=${record.state})")
            record.release()
            return
        }

        audioRecord = record
        record.startRecording()
        _isActive.value = true

        captureJob = scope.launch {
            val buffer = ByteArray(frameBytes)
            var sequence = 0L
            try {
                while (isActive) {
                    val captured = readExact(record, buffer)
                    if (!captured) {
                        Log.w(TAG, "Audio read returned short or error; stopping")
                        break
                    }
                    val frame = CapturedFrame(
                        sequenceNumber = sequence++,
                        captureTimestampMs = System.currentTimeMillis(),
                        sampleRateHz = sampleRateHz,
                        pcm16Mono = buffer.copyOf()
                    )
                    val accepted = _frames.tryEmit(frame)
                    if (!accepted) {
                        // Buffer overflow strategy is DROP_OLDEST; tryEmit only returns false
                        // on buffer pressure when DROP_OLDEST is set + no subscribers, which
                        // we still count as a dropped delivery for observability.
                        _droppedFrames.value = _droppedFrames.value + 1
                    }
                }
            } catch (t: Throwable) {
                Log.e(TAG, "Capture loop error", t)
            } finally {
                _isActive.value = false
            }
        }
    }

    fun stop() {
        captureJob?.cancel()
        captureJob = null
        audioRecord?.runCatching {
            if (state == AudioRecord.STATE_INITIALIZED) stop()
            release()
        }
        audioRecord = null
        _isActive.value = false
    }

    private fun readExact(record: AudioRecord, buffer: ByteArray): Boolean {
        var offset = 0
        while (offset < buffer.size) {
            val read = record.read(buffer, offset, buffer.size - offset)
            if (read <= 0) return false
            offset += read
        }
        return true
    }

    companion object {
        private const val TAG = "AudioFrameProducer"
        private const val BYTES_PER_SAMPLE = 2
    }
}
