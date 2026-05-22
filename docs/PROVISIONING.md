# Device Provisioning

This document covers turning a factory-fresh Android phone into a dedicated, locked-down drone audio sensor.

## Phase 1 — One-time setup (admin laptop)

```bash
# Install the provisioning CLI
cd scripts
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-gcp-project
```

## Phase 2 — Make the app the device owner (per phone)

Device Owner mode is the Android equivalent of "this app gets to run the device." Setting it up requires **factory-reset hardware** because Device Owner can only be granted via ADB (or NFC / QR code) on a device with no Google accounts added yet.

1. **Factory reset** the phone. Skip every "add a Google account" step during initial setup. Do NOT log in.
2. Enable Developer Options → enable USB debugging.
3. Plug in to the admin laptop.
4. Sideload the signed app APK:
   ```bash
   adb install -r app-release.apk
   ```
5. Set the app as device owner:
   ```bash
   adb shell dpm set-device-owner \
       com.dronesensor.app/.admin.DeviceOwnerReceiver
   ```
   You should see `Success: Active admin set to component {com.dronesensor.app/com.dronesensor.app.admin.DeviceOwnerReceiver}`.
6. Disconnect ADB.

The app is now permanent — it cannot be uninstalled by an ordinary user, it auto-launches on boot, and lock-task mode pins it as the foreground app.

## Phase 3 — Export the device's public key

After the app's first launch, its `DeviceIdentity` will have generated an EC P-256 keypair inside the Android Keystore. The private key never leaves the device. You need to pull the public key off for registration.

There are two ways to do this. Pick one:

### Option A: ADB pull (development / lab phones)

Add a temporary debug button or use `adb shell run-as` to dump the key:

```bash
adb shell run-as com.dronesensor.app \
    cat /data/data/com.dronesensor.app/files/public_key.pem
```

(This requires building a debuggable APK and adding a small `writePublicKeyPemForExport()` helper — out of scope for production.)

### Option B: On-device QR code

A future enhancement (not in v0.1.0): MainActivity renders the public-key PEM as a QR code that the admin scans with the provisioning laptop. Lower-friction than ADB pull.

For now, paste the PEM from logs:

```bash
adb logcat -d -s DeviceIdentity | grep "PUBLIC KEY" -A 4
```

Save the output as `device_001.pub.pem`.

## Phase 4 — Register the public key in Firestore

```bash
python scripts/provision_device.py register DRONE-SENSOR-001 \
    --pubkey device_001.pub.pem \
    --site "Site A"
```

The script writes:

```json
{
  "device_id": "DRONE-SENSOR-001",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----...",
  "status": "active",
  "assigned_site_label": "Site A",
  "registered_at_ms": 1735692000000
}
```

At this point the gateway (once `GATEWAY_REQUIRE_AUTH=true`) will accept JWTs from this device.

## Phase 5 — Configure the gateway endpoint

The app reads its endpoint from `BuildConfig` defaults plus SharedPreferences overrides. For an apk-baked endpoint:

```kotlin
// android/app/build.gradle.kts
buildConfigField("String", "DEFAULT_GRPC_HOST", "\"drone-sensor-dev-gateway-xxxxx-uc.a.run.app\"")
buildConfigField("int", "DEFAULT_GRPC_PORT", "443")
buildConfigField("boolean", "DEFAULT_TLS", "true")
```

For runtime override, use the JWT-audience SharedPreference (see `AppConfig.jwtAudience`) — must match the gateway's `GATEWAY_JWT_AUDIENCE` env var, which is normally the Cloud Run URL.

## Phase 6 — Verify

```bash
# Check the device is registered
python scripts/provision_device.py info DRONE-SENSOR-001

# Watch the gateway logs as the phone connects
gcloud run services logs read drone-sensor-dev-gateway --region=us-central1 --limit=50

# Watch frames hit Pub/Sub if a drone-like sound is detected
gcloud pubsub subscriptions pull drone-detections-debug --auto-ack --limit=5
```

## Decommissioning

```bash
python scripts/provision_device.py revoke DRONE-SENSOR-001 \
    --reason "removed from service"
```

Within ~5 minutes (the gateway public-key cache TTL), the device's future connections will be refused. For an immediate cutover, restart the gateway: `gcloud run services update drone-sensor-dev-gateway --region=us-central1 --update-env-vars=_REDEPLOY=$(date +%s)`.

## Bulk provisioning

For a 300-device rollout, drive `provision_device.py register` from a CSV:

```bash
while IFS=, read -r device_id pubkey_path site; do
    python scripts/provision_device.py register "$device_id" \
        --pubkey "$pubkey_path" --site "$site"
done < devices.csv
```

For zero-touch enrollment with NFC / QR / Knox / DPC identifiers, see [MDM_ENROLLMENT.md](MDM_ENROLLMENT.md).
