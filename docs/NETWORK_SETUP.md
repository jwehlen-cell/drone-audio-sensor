# Network Setup and Connectivity Watchdog

The phones are deployed by installers who should not need ADB, the
Android Settings menu, admin credentials, or technical troubleshooting.
This doc explains how a freshly-imaged phone gets onto cellular or
Starlink Wi-Fi, and how the app keeps itself online afterwards.

## Installer experience at a glance

1. Power the phone on.
2. The app starts on boot. Kiosk lock is **deferred** until first cloud
   check-in.
3. The app silently tries cellular for ~30 s. If a SIM is present and
   the cellular network resolves to the gateway, the device flips to
   `active` and locks into kiosk mode. **The installer does nothing.**
4. If cellular doesn't come up in time, the app shows a Wi-Fi setup
   card with:
   - current connection phase (color-coded dot + label)
   - SIM / cellular status
   - **Scan Wi-Fi** button → drop-down of visible SSIDs
   - password field + **Join Wi-Fi** button
5. The installer taps the Starlink SSID, types the password, taps
   **Join Wi-Fi**.
6. Once any path (cellular *or* Wi-Fi) carries a successful gRPC
   `SessionAck` from the gateway, the app:
   - persists `setup_complete = true` in SharedPreferences
   - calls `KioskController.enterLockTask()` (Device Owner installs)
   - switches the UI to the active-mode status view
7. From this point on, the installer is done — they can walk away.

The Wi-Fi card stays open if Wi-Fi setup fails or the password is
wrong; the installer can retry. The whole time, the app continues to
watch for cellular signal — if cellular comes online before Wi-Fi
finishes, the cellular path wins and Wi-Fi setup is bypassed.

## State during setup

The Firestore device document begins in `state = setup_pending` when
the admin calls `provision_device.py register ...`. The gateway
*allows* a setup_pending device to authenticate (it must, otherwise
first-check-in is impossible) but **does not publish its audio frames
to Redis**. On the first successful handshake the gateway atomically
flips the state to `active` in a Firestore transaction
(`registry.complete_setup`), recording:

- `setup_completed_at_ms`
- `setup_completed_session_id`

The admin status page distinguishes `setup_pending` (blue) from
`active` (green) — see the styles in `backend/admin/src/admin/static/
styles.css`.

## Android components

| Component | File | Responsibility |
|---|---|---|
| `ConnectivityMonitor` | `setup/ConnectivityMonitor.kt` | Composes Android NetworkCapabilities, SIM presence, and stream state into a single `ConnectionPhase` StateFlow. |
| `WifiSetupHelper` | `setup/WifiSetupHelper.kt` | Scans available SSIDs (`WifiManager.scanResults`), submits `WifiNetworkSuggestion` for the chosen SSID. |
| `ConnectivityWatchdog` | `setup/ConnectivityWatchdog.kt` | Reboots the phone via `DevicePolicyManager.reboot()` if cloud connectivity is missing for >5 min. Falls back to service restart on non-Device-Owner installs. |
| `MainActivity` | `MainActivity.kt` | Renders setup vs status sub-tree; gates `KioskController.enterLockTask()` on `AppConfig.setupComplete`. |
| `AppConfig` | `config/AppConfig.kt` | Persists `setupComplete`, `lastRebootMs`, `lastRebootReason`. |
| `StreamingClient` | `stream/StreamingClient.kt` | Flips `_firstAuthSeen` on first `SessionAck`/`FrameAck` and writes `setupComplete=true`. |

### `ConnectionPhase` values

```
NO_NETWORK              red
NO_SIM_NO_WIFI          red
SEARCHING_CELLULAR      yellow
CELLULAR_AVAILABLE      yellow
WIFI_AVAILABLE          yellow
WIFI_CONNECTED          yellow
CLOUD_REACHABLE         yellow
CLOUD_AUTHENTICATED     green       ── triggers setup completion
```

### Wi-Fi API choice

`WifiNetworkSuggestion` (Android 10+) is the right tool here:

- On **Device Owner** installs, the OS auto-approves the suggestion and
  auto-connects. The installer sees no system prompts.
- On non-Device-Owner installs, the user gets a one-time approval
  dialog the first time the app submits a suggestion. Fine for
  developer phones, not used in production deployments.

The legacy `WifiManager.addNetwork()` / `enableNetwork()` path was
deprecated in Android 10 and is silently a no-op for non-system apps,
so the helper does not fall back to it.

Wi-Fi scan results require runtime `ACCESS_FINE_LOCATION` — already
requested by `MainActivity` for the existing location-collection
feature.

## Connectivity watchdog

Anytime the phone is connected to the cloud (last `SessionAck` or
`FrameAck` received), the watchdog timer is reset. If more than
`AppConfig.connectivityRebootTimeoutMs` (default 5 min) elapses
without a successful auth:

1. The watchdog computes `since_auth_ms` for the reboot reason.
2. It checks the cooldown: `now - lastRebootMs >= minRebootIntervalMs`
   (default 10 min). If not, log and skip.
3. It writes `lastRebootMs = now` and `lastRebootReason = "no_cloud_<seconds>s"`.
4. **Device Owner path:** calls
   `DevicePolicyManager.reboot(DeviceOwnerReceiver.componentName)`.
5. **Fallback path** (no Device Owner): stops + restarts
   `AudioCaptureService`. Logged as a softer reset.
6. On the next successful check-in, the app sends a `DeviceHealth`
   message that includes `last_reboot_timestamp_ms` and
   `last_reboot_reason`. The admin dashboard renders the most recent
   reboot reason next to the device's row.

The cooldown is what prevents a fast reboot loop: if the post-reboot
connection still fails, the device will sit waiting another 10 minutes
before it tries again. The watchdog also runs on a 30 s tick instead
of polling continuously.

## Limitations and edge cases

- **Cellular without data plan:** the phone may show a working SIM and
  a tower attachment, but data calls fail. The monitor treats this as
  `SEARCHING_CELLULAR` because `NET_CAPABILITY_VALIDATED` will not be
  set. The fallback Wi-Fi UI still appears after the cellular grace
  window.
- **Captive-portal Wi-Fi:** Android marks the network as connected but
  not validated; the monitor stays at `WIFI_CONNECTED` and the gRPC
  attempt fails. The watchdog will eventually reboot, but the installer
  should ideally not be using captive Wi-Fi. The Starlink router is
  not captive.
- **Phone with no SIM at all:** SIM state is checked via
  `TelephonyManager.simState`. `ConnectionPhase.NO_SIM_NO_WIFI` is the
  terminal "needs installer help" state.
- **Watchdog cannot reboot:** on non-Device-Owner installs,
  `DevicePolicyManager.reboot()` throws. We catch the exception, log
  `dpm_reboot_failed`, and restart the service. The phone may be stuck
  unable to recover from certain pathological network states without a
  manual power cycle — this is a known limitation of dev builds.

## Operator-side visibility

Each setup-completion + each reboot lands in the admin dashboard:

- The "Registered phones" page shows `state = setup_pending` (blue
  pill) until the device first checks in.
- The handshake the gateway records also includes the latest
  `DeviceHealth` payload — `last_reboot_timestamp_ms` and
  `last_reboot_reason` are persisted on the Firestore device doc
  (existing `app_version` / `last_handshake_ms` pattern), so the
  Status page can surface "rebooted 7 min ago: no_cloud_312s" once
  the device reconnects.

The provisioning CLI also marks fresh registrations as
`setup_pending`:

```
$ python scripts/provision_device.py register DRONE-SENSOR-099 \
      --pubkey device_099.pub.pem --site "Site C"
Registered DRONE-SENSOR-099 (site=Site C) — state=setup_pending
```

To force-promote a device (bypass the cloud check-in, e.g. when
testing without a phone):

```
python scripts/provision_device.py set-state DRONE-SENSOR-099 active
```
