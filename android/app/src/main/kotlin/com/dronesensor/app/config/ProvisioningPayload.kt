package com.dronesensor.app.config

import com.dronesensor.app.BuildConfig
import com.dronesensor.app.identity.DeviceIdentity
import org.json.JSONObject

/**
 * Canonical shape of a "provision this phone" instruction.
 *
 * Today this object is constructed once at first launch from BuildConfig
 * constants set at build time via gradle properties:
 *
 *   ./gradlew installDebug \
 *       -PgrpcHost=drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app \
 *       -PgrpcPort=443 \
 *       -Ptls=true \
 *       -Psite=Patrick \
 *       -PsiteLabel="Patrick (emulator)" \
 *       -PdeviceId=PHONE-PATRICK-001
 *
 * Tomorrow the same shape will arrive via QR code: the installer scans
 * a tag, the scanner returns the encoded JSON, the app calls
 * [fromJson] to parse it, then [apply] to write everything into
 * SharedPreferences. No other code in the app cares which transport
 * delivered the payload — both produce the same in-memory object.
 *
 * QR payload JSON shape (versioned so the field set can evolve):
 *
 *   {
 *     "v": 1,
 *     "host": "drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app",
 *     "port": 443,
 *     "tls": true,
 *     "site": "Patrick",
 *     "site_label": "Patrick (emulator)",
 *     "device_id": "PHONE-PATRICK-001",
 *     "jwt_audience": ""
 *   }
 */
data class ProvisioningPayload(
    val host: String,
    val port: Int,
    val useTls: Boolean,
    val site: String,
    val siteLabel: String,
    val deviceId: String,
    val jwtAudience: String,
) {

    /**
     * Write everything into the app's persistent state. Idempotent:
     * re-applying the same payload is a no-op.
     */
    fun apply(appConfig: AppConfig, deviceIdentity: DeviceIdentity) {
        appConfig.endpoint = StreamEndpoint(host = host, port = port, useTls = useTls)
        appConfig.site = site
        appConfig.siteLabel = siteLabel
        appConfig.jwtAudience = jwtAudience
        if (deviceId.isNotBlank()) {
            deviceIdentity.overrideDeviceId(deviceId)
        }
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("v", SCHEMA_VERSION)
        put("host", host)
        put("port", port)
        put("tls", useTls)
        put("site", site)
        put("site_label", siteLabel)
        put("device_id", deviceId)
        put("jwt_audience", jwtAudience)
    }

    companion object {
        const val SCHEMA_VERSION = 1

        /**
         * Build a payload from the values baked into BuildConfig at
         * build time. Returns null when no provisioning data was
         * passed via gradle properties (i.e. the build used the
         * factory-default emulator-loopback fallback).
         */
        fun fromBuildConfig(): ProvisioningPayload? {
            // Treat "any non-default field present" as a provisioning
            // intent. The all-defaults case (10.0.2.2 + empty strings)
            // means "no provisioning supplied at build time."
            val hasSite = BuildConfig.DEFAULT_SITE.isNotBlank()
            val hasDeviceId = BuildConfig.DEFAULT_DEVICE_ID.isNotBlank()
            val nonLoopbackHost = BuildConfig.DEFAULT_GRPC_HOST != "10.0.2.2"
            if (!hasSite && !hasDeviceId && !nonLoopbackHost) return null
            return ProvisioningPayload(
                host = BuildConfig.DEFAULT_GRPC_HOST,
                port = BuildConfig.DEFAULT_GRPC_PORT,
                useTls = BuildConfig.DEFAULT_TLS,
                site = BuildConfig.DEFAULT_SITE,
                siteLabel = BuildConfig.DEFAULT_SITE_LABEL,
                deviceId = BuildConfig.DEFAULT_DEVICE_ID,
                jwtAudience = BuildConfig.DEFAULT_JWT_AUDIENCE,
            )
        }

        /** Parse a QR-delivered JSON string. Throws on malformed input. */
        fun fromJson(text: String): ProvisioningPayload {
            val o = JSONObject(text)
            val v = o.optInt("v", 0)
            require(v == SCHEMA_VERSION) {
                "Unsupported provisioning payload version: $v (expected $SCHEMA_VERSION)"
            }
            return ProvisioningPayload(
                host = o.getString("host"),
                port = o.getInt("port"),
                useTls = o.getBoolean("tls"),
                site = o.optString("site", ""),
                siteLabel = o.optString("site_label", ""),
                deviceId = o.optString("device_id", ""),
                jwtAudience = o.optString("jwt_audience", ""),
            )
        }
    }
}
