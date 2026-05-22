package com.dronesensor.app.identity

import android.util.Base64
import org.json.JSONObject

/**
 * Minimal ES256 JWS signer that uses an Android Keystore-backed identity.
 *
 * Avoids pulling in a JWT library — we build the compact JWS by hand:
 *   base64url(header) "." base64url(claims) "." base64url(signature)
 *
 * Header alg = ES256 (ECDSA P-256 SHA-256). Signature is the JWS-mandated
 * raw R||S form (64 bytes for P-256), NOT the ASN.1 DER form Android
 * returns by default. The conversion lives in DeviceIdentity.signSha256Raw.
 */
class JwtSigner(private val identity: DeviceIdentity) {

    fun sign(
        audience: String,
        issuer: String = "drone-sensor",
        ttlSeconds: Int = 300,
        extraClaims: Map<String, Any> = emptyMap(),
    ): String {
        val now = System.currentTimeMillis() / 1000
        val claims = JSONObject().apply {
            put("iss", issuer)
            put("sub", identity.deviceId)
            put("aud", audience)
            put("iat", now)
            put("exp", now + ttlSeconds)
            put("kid", identity.deviceId)
            extraClaims.forEach { (k, v) -> put(k, v) }
        }
        val header = JSONObject().apply {
            put("alg", "ES256")
            put("typ", "JWT")
            put("kid", identity.deviceId)
        }

        val headerB64 = b64url(header.toString().toByteArray(Charsets.UTF_8))
        val claimsB64 = b64url(claims.toString().toByteArray(Charsets.UTF_8))
        val signingInput = "$headerB64.$claimsB64".toByteArray(Charsets.US_ASCII)
        val signature = identity.signSha256Raw(signingInput)
        val sigB64 = b64url(signature)
        return "$headerB64.$claimsB64.$sigB64"
    }

    private fun b64url(bytes: ByteArray): String =
        Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
}
