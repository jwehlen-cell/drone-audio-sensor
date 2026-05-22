# Device Lifecycle

Every registered phone moves through a small state machine. The
canonical state lives in the device's Firestore document at
`devices/{id}.state` and is read by:

- the **gateway** on every connection, to decide whether to accept the
  stream and whether to forward audio to the inference pipeline;
- the **admin UI** to render the action buttons on the Registered
  Phones page;
- the **provisioning CLI** (`scripts/provision_device.py`) for the same
  reason as the admin UI.

## States

| state            | Connectable? | Audio forwarded? | Notes |
|------------------|--------------|------------------|-------|
| `setup_pending`  | yes          | **no**           | Freshly registered device. Phone is allowed to authenticate (so it can check in), but audio is held until first cloud check-in promotes the device to `active`. Kiosk lock is NOT engaged on the phone in this state — the installer can still join Starlink Wi-Fi. |
| `active`         | yes          | yes              | Normal device. |
| `lost`           | yes          | **no**           | Phone is missing or otherwise can't be trusted to broadcast audio. Health + location keep flowing so the device can still be tracked. |
| `revoked`        | no           | n/a              | Gateway refuses the JWT outright (see SECURITY.md). |
| `wipe_requested` | yes (once)   | **no**           | Gateway dispatches `CONTROL_TYPE_WIPE_DEVICE` on next connect, then flips state to `wipe_sent`. |
| `wipe_sent`      | no (terminal)| n/a              | Phone has been issued a factory reset. Re-enrolling requires a fresh `register` call with a new key. |

## Transition matrix

Admin-driven (via the UI or `provision_device.py set-state`):

```
setup_pending     -> { active, revoked, wipe_requested }   ── force-promote
active            -> { lost, revoked, wipe_requested }
lost              -> { active, revoked, wipe_requested }
revoked           -> { active }                 ── extra confirmation required
wipe_requested    -> { }                        ── only gateway can flip
wipe_sent         -> { }                        ── terminal
```

Gateway-internal:

```
setup_pending     -> active                     ── on first successful auth/handshake
wipe_requested    -> wipe_sent                  ── after a successful wipe dispatch
```

Transitions requiring **extra confirmation** in the admin UI / CLI:

- `revoked` → `active` (re-enabling a previously revoked device)
- `active` → `wipe_requested` (irreversible factory reset)
- `lost` → `wipe_requested` (same)
- `setup_pending` → `wipe_requested` (same)

## Behavior at the gateway

When a device connects, the gateway:

1. Validates the JWT against the registered public key.  
   `revoked` and `wipe_sent` devices fail this step (the registry's
   `get_public_key` returns `None` for them) → `UNAUTHENTICATED`.
2. Reads the lifecycle state via `registry.get_state(device_id)`.
3. **If `setup_pending`:** runs `complete_setup()` (Firestore txn) which
   flips the state to `active` and records `setup_completed_at_ms` +
   `setup_completed_session_id`. The rest of the session continues
   as if the device were active — audio publishes normally.
4. **If `wipe_requested`:** dispatches a single
   `ServerCommand(control = ControlCommand(type=CONTROL_TYPE_WIPE_DEVICE))`
   on the stream, atomically transitions Firestore to `wipe_sent`,
   stamps `wipe_sent_at_ms` and `last_wipe_session_id`, and closes the
   stream. The next connect from that device will be rejected at auth.
5. **If `lost`:** accepts handshake, health, and location updates, but
   never calls `state.publish_frame()`. The device shows up on the
   admin status page as connected (yellow if recently lost, red if
   stale).
6. **If `active`:** normal pipeline.

The admin Firestore writes go through a **transaction** so a user
clicking "wipe" at the same moment the gateway is processing a
wipe_requested → wipe_sent flip can't race past each other.

## Behavior on the phone

`CONTROL_TYPE_WIPE_DEVICE` is dispatched by
`StreamingClient.handleServerCommand` to `WipeHandler.execute()`. The
handler:

1. Checks `DevicePolicyManager.isDeviceOwnerApp(packageName)`. If the
   app is **not** the Device Owner, it logs a warning and returns. This
   protects sideloaded developer phones from accidental wipes.
2. On a Device Owner install, calls
   `DevicePolicyManager.wipeData(WIPE_EXTERNAL_STORAGE |
   WIPE_RESET_PROTECTION_DATA, reason)` which triggers the system
   factory-reset flow.

There is no client-side ack of the wipe — the gateway flips
`wipe_requested → wipe_sent` as soon as it has *dispatched* the
command, on the assumption that we will not get a clean response from
a phone in the middle of being wiped. Subsequent connects from that
phone (if any happen before the wipe completes) fail at auth.

## Admin UI flow

| Page | Path | What it does |
|------|------|--------------|
| Status | `/` | Live device states from Redis, joined to Firestore registration, plus detections from the last hour. |
| Registered phones | `/registered` | All devices, with per-device action buttons for the allowed transitions. Buttons that need extra confirmation use a `window.prompt` requiring the literal string `WIPE` (for wipe) or a `window.confirm` (for revoked → active). |

`wipe_sent` devices show their action column as "no actions" — they
are terminal in the UI.

## Common operations

```bash
# Mark a phone as lost (still allowed to connect for location).
python scripts/provision_device.py set-state DRONE-SENSOR-042 lost

# Return a lost phone to normal.
python scripts/provision_device.py set-state DRONE-SENSOR-042 active

# Revoke a phone (gateway will reject all future connects).
python scripts/provision_device.py revoke DRONE-SENSOR-042

# Re-enable a previously revoked phone (extra confirmation required).
python scripts/provision_device.py set-state DRONE-SENSOR-042 active --confirm

# Queue a remote wipe on next connect (irreversible).
python scripts/provision_device.py request-wipe DRONE-SENSOR-042 --confirm WIPE

# Inspect current state.
python scripts/provision_device.py info DRONE-SENSOR-042
```
