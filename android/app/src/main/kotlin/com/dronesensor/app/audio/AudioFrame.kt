package com.dronesensor.app.audio

data class CapturedFrame(
    val sequenceNumber: Long,
    val captureTimestampMs: Long,
    val sampleRateHz: Int,
    val pcm16Mono: ByteArray
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is CapturedFrame) return false
        return sequenceNumber == other.sequenceNumber &&
                captureTimestampMs == other.captureTimestampMs &&
                sampleRateHz == other.sampleRateHz &&
                pcm16Mono.contentEquals(other.pcm16Mono)
    }

    override fun hashCode(): Int {
        var result = sequenceNumber.hashCode()
        result = 31 * result + captureTimestampMs.hashCode()
        result = 31 * result + sampleRateHz
        result = 31 * result + pcm16Mono.contentHashCode()
        return result
    }
}
