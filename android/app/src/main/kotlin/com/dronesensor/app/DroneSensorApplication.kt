package com.dronesensor.app

import android.app.Application
import android.util.Log
import com.dronesensor.app.config.AppConfig
import com.dronesensor.app.config.ProvisioningPayload
import com.dronesensor.app.identity.DeviceIdentity

class DroneSensorApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        val config = AppConfig.get(this)
        val identity = DeviceIdentity.get(this)
        applyBuildTimeProvisioning(config, identity)
    }

    /**
     * One-shot, run-once provisioning at first launch. If the build
     * carried a ProvisioningPayload (via gradle -P overrides), apply
     * it to SharedPreferences and the device identity. Subsequent
     * launches are no-ops: the BUILD_PROVISIONED guard flag in
     * SharedPreferences keeps re-installs from clobbering a payload
     * the operator may have updated post-install (e.g. via a future
     * QR scan).
     */
    private fun applyBuildTimeProvisioning(
        config: AppConfig,
        identity: DeviceIdentity,
    ) {
        if (config.buildProvisioned) return
        val payload = ProvisioningPayload.fromBuildConfig() ?: return
        Log.i(
            "DroneSensorApp",
            "applying build-time provisioning: host=${payload.host}:${payload.port} " +
                "tls=${payload.useTls} site=${payload.site} device=${payload.deviceId}",
        )
        payload.apply(config, identity)
        config.buildProvisioned = true
    }
}
