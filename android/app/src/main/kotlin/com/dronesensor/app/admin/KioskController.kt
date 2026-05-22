package com.dronesensor.app.admin

import android.app.Activity
import android.app.admin.DevicePolicyManager
import android.content.Context
import android.content.IntentFilter
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Wraps the Device Owner / lock-task plumbing.
 *
 * Provisioning is a one-time, out-of-band step that puts this app in
 * device-owner state on a factory-fresh phone:
 *
 *     adb shell dpm set-device-owner \
 *         com.dronesensor.app/.admin.DeviceOwnerReceiver
 *
 * Once the app is device owner, this controller can:
 *   - Add itself to the lock-task allowlist
 *   - Pin itself as the system launcher so HOME stays inside the app
 *   - Disable status bar / safe boot (defense in depth for unattended kiosks)
 *
 * Methods are safe to call when the app is not a device owner: they log
 * and return without crashing.
 */
class KioskController(private val context: Context) {

    private val dpm: DevicePolicyManager? =
        ContextCompat.getSystemService(context, DevicePolicyManager::class.java)

    private val admin = DeviceOwnerReceiver.componentName(context)

    val isDeviceOwner: Boolean
        get() = dpm?.isDeviceOwnerApp(context.packageName) == true

    fun applyKioskPolicies() {
        val mgr = dpm ?: return
        if (!isDeviceOwner) {
            Log.i(TAG, "Not device owner — skipping kiosk policies")
            return
        }
        runCatching {
            mgr.setLockTaskPackages(admin, arrayOf(context.packageName))
        }.onFailure { Log.w(TAG, "setLockTaskPackages failed: ${it.message}") }

        runCatching {
            mgr.addPersistentPreferredActivity(
                admin,
                IntentFilter(android.content.Intent.ACTION_MAIN).apply {
                    addCategory(android.content.Intent.CATEGORY_HOME)
                    addCategory(android.content.Intent.CATEGORY_DEFAULT)
                },
                android.content.ComponentName(context.packageName, "com.dronesensor.app.MainActivity"),
            )
        }.onFailure { Log.w(TAG, "addPersistentPreferredActivity failed: ${it.message}") }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            runCatching {
                mgr.setKeyguardDisabled(admin, true)
            }.onFailure { Log.w(TAG, "setKeyguardDisabled failed: ${it.message}") }
        }
    }

    fun enterLockTask(activity: Activity) {
        if (!isDeviceOwner) return
        runCatching { activity.startLockTask() }
            .onFailure { Log.w(TAG, "startLockTask failed: ${it.message}") }
    }

    fun exitLockTask(activity: Activity) {
        runCatching { activity.stopLockTask() }
            .onFailure { Log.w(TAG, "stopLockTask failed: ${it.message}") }
    }

    companion object {
        private const val TAG = "KioskController"
    }
}
