# Security Model

## Threat model

| # | Threat | Mitigation |
|---|---|---|
| T1 | Anyone on the internet streams audio into the pipeline | Per-device JWT auth (ES256, Keystore-backed) — gateway rejects on signature/sub mismatch |
| T2 | Stolen phone replays old captured audio | Stale ACK/timestamps allowed but the device key is bound to the phone; suppression window limits damage |
| T3 | Extracted device private key used elsewhere | Keys are generated in `AndroidKeyStore` with StrongBox preferred — extraction requires physical attack on the secure element |
| T4 | Lost / compromised phone keeps streaming | Admin moves device to `lost` (location-only) or `revoked` via the admin UI or `provision_device.py set-state`; gateway clears its 5-minute public-key cache after TTL and rejects from then on |
| T4a | Stolen Device Owner phone deserves a forced wipe | Admin transitions device to `wipe_requested`; gateway issues `CONTROL_TYPE_WIPE_DEVICE` on next connect and atomically flips state to `wipe_sent`; phone calls `DevicePolicyManager.wipeData` only if it is the Device Owner |
| T5 | Eavesdropping on phone↔cloud audio | TLS at the Cloud Run edge (HTTPS+gRPC) |
| T6 | Eavesdropping on TAK publisher↔TAK Server | Persistent TLS client cert connection; certs in Secret Manager |
| T7 | TAK Server credential exfiltration | Stored only in Secret Manager; `secretAccessor` IAM scoped to the publisher SA; PEM written to chmod-600 tempfile and deleted after `load_cert_chain` |
| T8 | Unauthorized backend reads | All services run as dedicated least-privilege SAs; no shared default SAs touched |
| T9 | Unauthorized backend writes | Datastore.user is the only Firestore role granted; no admin role anywhere; provisioning script uses ADC, not a service account key |
| T10 | Detection event impersonation upstream of TAK publisher | Pub/Sub message integrity guaranteed by GCP; publisher subscription has a dedicated, scoped SA |

## Auth flow (device → gateway)

```
+---------------------+                              +-----------------------+
|  Android phone      |                              |  Cloud Run gateway    |
|                     |                              |                       |
|  Keystore private   |                              |                       |
|  key (EC P-256)     |                              |                       |
|                     |--- gRPC + Authorization ---->|                       |
|  signs JWT          |    Bearer <ES256 JWT>        |                       |
|  per connection     |                              |  decode header → kid  |
|                     |                              |        |              |
|                     |                              |        v              |
|                     |                              |  Firestore lookup     |
|                     |                              |  devices/{kid}.       |
|                     |                              |  public_key_pem       |
|                     |                              |        |              |
|                     |                              |        v              |
|                     |                              |  jwt.decode(token,    |
|                     |                              |    public_key,        |
|                     |                              |    algorithms=[ES256])|
|                     |                              |        |              |
|                     |                              |        v              |
|                     |<------ stream accepted ------|  + handshake.device_id|
|                     |        OR UNAUTHENTICATED    |    must equal sub     |
+---------------------+                              +-----------------------+
```

### JWT claims

| Claim | Value | Notes |
|---|---|---|
| `iss` | `drone-sensor` | Hardcoded issuer |
| `sub` | `DRONE-SENSOR-xxxxxxxx` | Same as `kid` and as handshake `device_id` |
| `aud` | gateway URL | e.g. `https://drone-sensor-dev-gateway-xxxxx-uc.a.run.app` |
| `iat` | unix seconds | Issued-at |
| `exp` | iat + 300 | 5-minute TTL — a fresh token is minted per gRPC channel |
| `kid` | device_id | Header claim, identifies which key to verify with |

### Key rotation

There is no shared CA — each device has its own keypair. To rotate:

1. On the device, generate a new keypair (delete the alias, restart app → it regenerates).
2. Export the new public key.
3. `provision_device.py register DEVICE-ID --pubkey new.pem` — overwrites the public key in Firestore.
4. The gateway cache TTL (5 min) is the maximum time the old key remains accepted; force eviction by restarting the gateway.

### Lifecycle states & revocation

Each device document carries a `state` field that the gateway honors on
every connect. The complete state machine — including the wipe-on-next
-connect flow — lives in [DEVICE_LIFECYCLE.md](DEVICE_LIFECYCLE.md).
Quick reference:

```
active           - normal device; can stream audio
lost             - location-only; gateway accepts handshake/health/
                   location but does NOT publish audio frames
revoked          - gateway refuses connections outright
wipe_requested   - on next connect, gateway sends CONTROL_TYPE_WIPE_DEVICE
                   then atomically flips state to wipe_sent
wipe_sent        - terminal; refuses further connections
```

Operator commands:

```
python provision_device.py set-state DRONE-SENSOR-001 lost
python provision_device.py set-state DRONE-SENSOR-001 revoked
python provision_device.py request-wipe DRONE-SENSOR-001 --confirm WIPE
```

Within at most `jwt_public_key_cache_seconds` (default 300 s) the
gateway will pick up the new state.

The admin UI ([backend/admin/](../backend/admin/)) exposes the same
transitions with extra-confirmation prompts on dangerous moves.

## Secrets inventory

| Secret | Storage | Read by | Rotation |
|---|---|---|---|
| Per-device EC private key | AndroidKeyStore (StrongBox if available) | the device itself | Re-provision + revoke old |
| Per-device EC public key | Firestore `devices/{id}.public_key_pem` | gateway SA | Tracked by re-provisioning |
| TAK Server client cert + key + CA | Secret Manager `<env>-tak-credentials` | tak-publisher SA | Update secret version + restart publisher |
| Device bootstrap material | Secret Manager `<env>-device-bootstrap` | gateway SA (reserved for future use) | n/a |

## Cloud Run authentication

The Cloud Run service for the gateway is **not** IAM-gated (`allowUnauthenticatedInvocations = true`). All access control is at the application layer — that's the JWT check above. This is by design:

- We can't use Cloud Run IAM because phones don't carry Google identities
- mTLS via Cloud Run + a Global LB + client cert auth is significantly more operational overhead
- App-layer JWTs let us scope, revoke, and rotate per device without GCP IAM changes

If your environment requires Cloud Run IAM as a defense-in-depth layer, switch `allowUnauthenticatedInvocations = false` and front the service with an internal Cloud Run proxy / API gateway that injects an ID token.

## Network exposure

| Service | Ingress | Egress |
|---|---|---|
| Gateway | Public (Cloud Run, HTTPS only) | VPC connector → Memorystore + Firestore via Google APIs |
| Inference worker | Internal-only | VPC connector → Memorystore + Pub/Sub |
| TAK publisher | Internal-only | All traffic (VPC connector ALL_TRAFFIC) — needs to reach external TAK Server |
| Firestore / Pub/Sub / Memorystore | Service-mesh (Private Service Access for Memorystore) | n/a |

## Known limitations

- The provisioning step is manual / scripted; there is no zero-touch enrollment yet. See [MDM_ENROLLMENT.md](MDM_ENROLLMENT.md) for the Android Management API approach.
- No HSM-backed signing on the gateway side — public-key crypto only verifies; nothing is signed by the gateway.
- TLS pinning is not implemented on the phone — the gateway uses standard system trust. Add cert pinning if your threat model includes operator-controlled CAs.
- The 5-minute key cache means a revoked device can continue streaming for up to 5 minutes. Tune `GATEWAY_JWT_PUBLIC_KEY_CACHE_SECONDS` lower if needed, at the cost of more Firestore reads.
