# MDM / Zero-Touch Enrollment

For 300 phones, manually ADB-provisioning each one is not the right answer. This document describes the recommended path: **Android Management API (AMAPI)** with a custom DPC-free enrollment.

## Why AMAPI

| Option | Verdict |
|---|---|
| **Android Management API** (Google) | Recommended. Handles enrollment, policy, app delivery, OTA app updates. No app needed to manage the device. |
| Third-party MDMs (Workspace ONE, Intune, Esper) | Fine, but adds a license + vendor dependency. |
| Knox Mobile Enrollment | Samsung-specific; useful if your hardware is Samsung. Can layer over AMAPI. |
| DIY (custom DPC + Play Store) | Don't. AMAPI replaced this in 2019. |

## High-level flow

```
   [admin]                  [Google]              [factory-fresh phone]
      |                        |                          |
      |--- create_enterprise ->|                          |
      |                        |                          |
      |--- create_policy ----->|                          |
      |    (kiosk + app)       |                          |
      |                        |                          |
      |--- create_enrollment_  |                          |
      |    token ------------->|                          |
      |    (returns QR JSON)   |                          |
      |                        |                          |
      |    print QR code <-----|                          |
      |                        |                          |
      |                        |   scan QR at setup ----->|
      |                        |<--- enroll --------------|
      |                        |                          |
      |                        |--- push policy --------->|
      |                        |--- install app --------->|
      |                        |                          |
      |                        |                          |  app starts,
      |                        |                          |  kiosk mode on,
      |                        |                          |  Keystore key gen
```

## One-time setup

### 1. Create an enterprise

You need a Google account (any) and the [Android Management API](https://developers.google.com/android/management/) enabled on your GCP project.

```bash
gcloud services enable androidmanagement.googleapis.com

# Use the signup URL helper to create an enterprise tied to your project
gcloud auth print-access-token
```

Then call `enterprises.create` via REST (the AMAPI sample app makes this much easier — see https://github.com/google/android-management-api-samples).

You'll end up with an `enterpriseName` like `enterprises/LC012ab345`.

### 2. Upload the app

You have two options:

| Option | When |
|---|---|
| **Managed Google Play (private)** | The app is published to Play but only your enterprise can install it |
| **Managed Google Play (web)** | The app isn't on Play; you upload an APK manually |

For a dedicated-device fleet I recommend **private Play distribution**: easier OTA updates, signature-verified, no APK URL hosting concerns.

```
https://play.google.com/work/apps/details?id=com.dronesensor.app
```

### 3. Define the policy

Save as `policy.json`:

```json
{
  "applications": [
    {
      "packageName": "com.dronesensor.app",
      "installType": "FORCE_INSTALLED",
      "lockTaskAllowed": true,
      "defaultPermissionPolicy": "GRANT",
      "permissionGrants": [
        { "permission": "android.permission.RECORD_AUDIO", "policy": "GRANT" },
        { "permission": "android.permission.ACCESS_FINE_LOCATION", "policy": "GRANT" },
        { "permission": "android.permission.ACCESS_BACKGROUND_LOCATION", "policy": "GRANT" },
        { "permission": "android.permission.POST_NOTIFICATIONS", "policy": "GRANT" }
      ]
    }
  ],
  "kioskCustomization": {
    "powerButtonActions": "POWER_BUTTON_AVAILABLE",
    "systemErrorWarnings": "ERROR_AND_WARNINGS_MUTED",
    "systemNavigation": "NAVIGATION_DISABLED",
    "statusBar": "NOTIFICATIONS_AND_SYSTEM_INFO_DISABLED",
    "deviceSettings": "SETTINGS_ACCESS_DISABLED"
  },
  "persistentPreferredActivities": [
    {
      "actions": ["android.intent.action.MAIN"],
      "categories": ["android.intent.category.HOME", "android.intent.category.DEFAULT"],
      "receiverActivity": "com.dronesensor.app/com.dronesensor.app.MainActivity"
    }
  ],
  "keyguardDisabled": true,
  "statusBarDisabled": true,
  "addUserDisabled": true,
  "factoryResetDisabled": false,
  "installAppsDisabled": true,
  "modifyAccountsDisabled": true,
  "mountPhysicalMediaDisabled": true,
  "tetheringConfigDisabled": true,
  "developerSettings": "DEVELOPER_SETTINGS_DISABLED",
  "screenCaptureDisabled": false,
  "networkEscapeHatchEnabled": true
}
```

Then push it:

```bash
ENTERPRISE=enterprises/LC012ab345
POLICY=$ENTERPRISE/policies/drone-sensor-kiosk

curl -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  --data-binary @policy.json \
  "https://androidmanagement.googleapis.com/v1/$POLICY"
```

### 4. Create an enrollment token

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  --data '{
    "policyName": "'$POLICY'",
    "allowPersonalUsage": "PERSONAL_USAGE_DISALLOWED",
    "oneTimeOnly": false
  }' \
  "https://androidmanagement.googleapis.com/v1/$ENTERPRISE/enrollmentTokens"
```

The response contains a `qrCode` field — a JSON blob you turn into a QR code (any QR generator works).

### 5. Provision phones

On each factory-fresh phone:

1. Tap the welcome screen **6 times** to launch the QR scanner.
2. Scan the QR.
3. The phone connects to Wi-Fi, downloads the Android Device Policy app, applies the policy, installs your app, and reboots into kiosk mode.

This takes 5–10 minutes per phone, fully automated.

## Provisioning the device JWT keypair

After AMAPI hands the device off to your app, the app generates its EC keypair on first run (see `DeviceIdentity`). You still need to **register the public key in Firestore** (see [PROVISIONING.md](PROVISIONING.md) Phase 3–4).

For zero-touch end-to-end:

- Have the app POST its public key (signed with itself, with a one-time pre-shared enrollment token from a separate Secret Manager secret) to a small "registration endpoint" Cloud Run service that writes to Firestore.
- The enrollment token is delivered as a policy `applicationConfig` extra:

```json
{
  "applicationConfig": {
    "com.dronesensor.app": {
      "enrollment_token": "<short-lived token from Secret Manager>"
    }
  }
}
```

This is an enhancement to consider once the manual flow is proven.

## Updating the fleet

App updates flow through Managed Google Play automatically. To force a faster rollout:

```bash
curl -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  --data '{ "appAutoUpdatePolicy": "ALWAYS" }' \
  "https://androidmanagement.googleapis.com/v1/$POLICY"
```

Policy edits propagate within minutes.

## Removal

Wiping a device:

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  --data '{ "type": "RESET_PASSWORD", "newPassword": "" }' \
  "https://androidmanagement.googleapis.com/v1/$ENTERPRISE/devices/<deviceId>:issueCommand"
```

Or the more aggressive `DELETE` on the device resource (factory reset).
