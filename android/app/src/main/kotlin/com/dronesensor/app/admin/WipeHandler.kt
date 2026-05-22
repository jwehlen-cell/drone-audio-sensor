package com.dronesensor.app.admin

import android.app.admin.DevicePolicyManager
import android.content.Context
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Executes a remote factory-reset on receipt of CONTROL_TYPE_WIPE_DEVICE.
 *
 * Only a Device Owner install can actually wipe. On non-owner installs
 * (developer phones, sideloaded test builds) this no-ops with a warning
 * so we don't kill an engineer's device by accident.
 *
 * The gateway flips the lifecycle state to `wipe_sent` as soon as it
 * dispatches the command — we don't need to ack from the device side.
 */
class WipeHandler(private val context: Context) {

    private val dpm: DevicePolicyManager? =
        ContextCompat.getSystemService(context, DevicePolicyManager::class.java)

    val isCapable: Boolean
        get() = dpm?.isDeviceOwnerApp(context.packageName) == true

    fun execute(reason: String = "remote-wipe-command") {
        val mgr = dpm
        if (mgr == null) {
            Log.w(TAG, "DevicePolicyManager unavailable; ignoring wipe request")
            return
        }
        if (!isCapable) {
            Log.w(
                TAG,
                "Wipe requested but app is not Device Owner; refusing to wipe",
            )
            return
        }
        val flags = WIPE_EXTERNAL_STORAGE or WIPE_RESET_PROTECTION_DATA
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                mgr.wipeData(flags, reason)
            } else {
                @Suppress("DEPRECATION")
                mgr.wipeData(flags)
            }
            Log.w(TAG, "wipeData() requested: reason=$reason")
        } catch (t: Throwable) {
            Log.e(TAG, "wipeData() failed: ${t.message}", t)
        }
    }

    companion object {
        private const val TAG = "WipeHandler"
        private const val WIPE_EXTERNAL_STORAGE: Int = 0x00000001
        private const val WIPE_RESET_PROTECTION_DATA: Int = 0x00000002
    }
}
