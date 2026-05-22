package com.dronesensor.app.stream

import kotlin.random.Random

class ReconnectPolicy(
    private val initialDelayMs: Long = 1_000,
    private val maxDelayMs: Long = 60_000
) {
    private var attempt: Int = 0
    val attempts: Int get() = attempt

    fun nextDelayMs(): Long {
        val capped = minOf(8, attempt)
        val base = minOf(maxDelayMs, initialDelayMs shl capped)
        attempt++
        val half = base / 2
        return half + Random.nextLong(half + 1)
    }

    fun reset() {
        attempt = 0
    }
}
