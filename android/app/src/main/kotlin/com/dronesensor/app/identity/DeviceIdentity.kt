package com.dronesensor.app.identity

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.core.content.edit
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.UUID

/**
 * Hardware-backed device identity.
 *
 * On first launch we generate an EC P-256 keypair inside `AndroidKeyStore`
 * (StrongBox when available). The private key never leaves the secure
 * element. A human-readable `device_id` is also persisted in shared
 * preferences and is the identifier the rest of the system uses; the
 * public key is what proves that identity to the gateway.
 *
 * Provisioning step: export the public-key PEM from this device, hand it
 * to the admin tooling, which writes it to Firestore under the device's
 * registry document. The gateway validates incoming JWTs against that
 * stored public key.
 */
class DeviceIdentity private constructor(context: Context) {

    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    val deviceId: String by lazy {
        prefs.getString(KEY_DEVICE_ID, null) ?: generateDeviceIdAndPersist()
    }

    init {
        ensureKeyPair()
    }

    fun overrideDeviceId(newId: String) {
        prefs.edit { putString(KEY_DEVICE_ID, newId) }
    }

    /** Returns the device public key in PEM (SubjectPublicKeyInfo) format. */
    fun publicKeyPem(): String {
        val publicKey = loadPublicKey()
        val encoded = publicKey.encoded
        val b64 = Base64.encodeToString(encoded, Base64.NO_WRAP)
        val wrapped = b64.chunked(64).joinToString("\n")
        return "-----BEGIN PUBLIC KEY-----\n$wrapped\n-----END PUBLIC KEY-----\n"
    }

    /** Sign `data` with the Keystore private key, returning ASN.1 DER bytes. */
    fun signSha256Der(data: ByteArray): ByteArray {
        val privateKey = loadPrivateKey()
        val sig = Signature.getInstance("SHA256withECDSA")
        sig.initSign(privateKey)
        sig.update(data)
        return sig.sign()
    }

    /**
     * Sign data and return the signature in the raw R||S form that JWS ES256
     * expects (64 bytes for P-256, left-padded with zeros).
     */
    fun signSha256Raw(data: ByteArray): ByteArray = derToRawEcdsa(signSha256Der(data))

    private fun generateDeviceIdAndPersist(): String {
        val id = "DRONE-SENSOR-" + UUID.randomUUID().toString().take(8).uppercase()
        prefs.edit { putString(KEY_DEVICE_ID, id) }
        return id
    }

    private fun ensureKeyPair() {
        val ks = KeyStore.getInstance(KEYSTORE).also { it.load(null) }
        if (ks.containsAlias(KEY_ALIAS)) return

        val kpg = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, KEYSTORE)
        val builder = KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
        )
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            // Try StrongBox first; not all devices have it.
            try {
                builder.setIsStrongBoxBacked(true)
                kpg.initialize(builder.build())
                kpg.generateKeyPair()
                return
            } catch (_: Throwable) {
                builder.setIsStrongBoxBacked(false)
            }
        }
        kpg.initialize(builder.build())
        kpg.generateKeyPair()
    }

    private fun loadPrivateKey(): PrivateKey {
        val ks = KeyStore.getInstance(KEYSTORE).also { it.load(null) }
        return ks.getKey(KEY_ALIAS, null) as PrivateKey
    }

    private fun loadPublicKey(): PublicKey {
        val ks = KeyStore.getInstance(KEYSTORE).also { it.load(null) }
        return ks.getCertificate(KEY_ALIAS).publicKey
    }

    companion object {
        private const val PREFS = "drone_sensor_identity"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_ALIAS = "drone_sensor_signing_key_v1"
        private const val KEYSTORE = "AndroidKeyStore"

        @Volatile private var instance: DeviceIdentity? = null

        fun get(context: Context): DeviceIdentity =
            instance ?: synchronized(this) {
                instance ?: DeviceIdentity(context).also { instance = it }
            }
    }
}

/**
 * Convert an ASN.1 DER ECDSA signature to fixed-length R||S form.
 * P-256 produces 64 bytes (32 R + 32 S).
 */
internal fun derToRawEcdsa(der: ByteArray, keySize: Int = 32): ByteArray {
    var off = 0
    require(der[off++] == 0x30.toByte()) { "Not a DER SEQUENCE" }
    // total length byte (may be multi-byte for >127, but ECDSA sigs are small)
    if (der[off].toInt() and 0x80 != 0) {
        val n = der[off++].toInt() and 0x7F
        off += n
    } else {
        off++
    }

    require(der[off++] == 0x02.toByte()) { "Expected INTEGER for R" }
    val rLen = der[off++].toInt() and 0xFF
    var r = der.copyOfRange(off, off + rLen)
    off += rLen

    require(der[off++] == 0x02.toByte()) { "Expected INTEGER for S" }
    val sLen = der[off++].toInt() and 0xFF
    var s = der.copyOfRange(off, off + sLen)

    if (r.isNotEmpty() && r[0] == 0.toByte() && r.size > keySize) {
        r = r.copyOfRange(1, r.size)
    }
    if (s.isNotEmpty() && s[0] == 0.toByte() && s.size > keySize) {
        s = s.copyOfRange(1, s.size)
    }

    val out = ByteArray(keySize * 2)
    System.arraycopy(r, 0, out, keySize - r.size, r.size)
    System.arraycopy(s, 0, out, keySize * 2 - s.size, s.size)
    return out
}
