# Argos UAT replay bridge

Pull historical SH-* clips from the prod Argos GCS bucket (read-only,
cross-project) and replay them into the argosuat gateway in real time
as if they were live phones. All PKI material is freshly minted,
sandbox-only, and labelled "TEST".

## One-time setup (in argosuat)

### 1. Bridge service account

```bash
gcloud iam service-accounts create argos-bridge \
  --project=argosuat \
  --display-name "Argos UAT pull bridge"

gcloud projects add-iam-policy-binding argosuat \
  --member=serviceAccount:argos-bridge@argosuat.iam.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor

gcloud projects add-iam-policy-binding argosuat \
  --member=serviceAccount:argos-bridge@argosuat.iam.gserviceaccount.com \
  --role=roles/datastore.user
```

### 2. Cross-project grants (run by someone with prod argos admin)

The bridge SA needs **two** read-only grants in argos-487318:

```bash
# Read clips out of the unzipped bucket
gcloud storage buckets add-iam-policy-binding \
  gs://aftac-argos-dataflow-unzipped \
  --project=argos-487318 \
  --member=serviceAccount:argos-bridge@argosuat.iam.gserviceaccount.com \
  --role=roles/storage.objectViewer

# Query the sensor location registry (per-clip GPS fallback)
gcloud projects add-iam-policy-binding argos-487318 \
  --member=serviceAccount:argos-bridge@argosuat.iam.gserviceaccount.com \
  --role=roles/bigquery.jobUser

gcloud projects add-iam-policy-binding argos-487318 \
  --member=serviceAccount:argos-bridge@argosuat.iam.gserviceaccount.com \
  --role=roles/bigquery.dataViewer
```

(`bigquery.jobUser` lets the bridge submit a query; `bigquery.dataViewer`
at the project level grants read on every dataset. If you'd rather
scope tighter, bind `dataViewer` on the `argos` dataset specifically.)

Without these grants the bridge will start cleanly but every station
will log 403 on `list_blobs` and `BigQuery registry load failed`.
Sidecar GPS resolution falls back to "no location available" and
clips get skipped.

### 3. Mint test PKI material

From a workstation authenticated to argosuat as a Secret Manager Admin:

```bash
.venv/bin/pip install -r scripts/requirements.txt
.venv/bin/python scripts/argos_bridge/mint_test_pki.py --project=argosuat
```

This creates the secrets:
- `argos-uat-test-ca-cert`, `argos-uat-test-ca-key`
- `argos-uat-sim-cert-<STATION>`, `argos-uat-sim-key-<STATION>` for each of 33 stations

Public keys are also written to `scripts/argos_bridge/out_pubkeys/<STATION>.pub.pem`
for the next step.

### 4. Enroll the 33 stations in Firestore

```bash
.venv/bin/python scripts/argos_bridge/enroll_stations.py --project=argosuat
```

Each station gets a device doc with `state=active`, `public_key_pem`,
and `current_location` set from the roster.

### 5. Provision the bridge VM

```bash
PROJECT=argosuat ZONE=us-west2-a bash scripts/argos_bridge/provision.sh
```

The startup script installs Python, clones the repo, builds the venv,
regenerates protos, and starts the `argos-bridge` systemd unit. First
boot takes ~3-5 min on e2-small (grpcio compiles from source).

## Verifying

```bash
# Are SH-* stations handshaking?
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="drone-sensor-dev-gateway"
   AND jsonPayload.event="handshake_received"
   AND jsonPayload.device_id:"SH"' \
  --project=argosuat --limit=10 --freshness=5m

# Status page
open https://drone-sensor-dev-admin-895207822840.us-west2.run.app/
```

## Smoke test

For a quick subset run before the full fleet:

```bash
BRIDGE_STATIONS=SH000,SH002 \
GOOGLE_CLOUD_PROJECT=argosuat \
.venv/bin/python scripts/argos_bridge/bridge.py
```

## Tunables (env vars on the systemd unit or shell)

| var | default | meaning |
|---|---|---|
| `BRIDGE_GATEWAY_URL` | argosuat gateway run URL | where to stream |
| `BRIDGE_GATEWAY_TLS` | true | Cloud Run terminates HTTPS |
| `BRIDGE_GCS_BUCKET` | `aftac-argos-dataflow-unzipped` | source bucket |
| `BRIDGE_GCS_PREFIX` | `ensco/SH` | per-station prefix root |
| `BRIDGE_WINDOW_HOURS` | 4 | how many hours of history per loop |
| `BRIDGE_CODEC` | `pcm16` | `pcm16` or `flac` |
| `BRIDGE_REFRESH_INTERVAL_S` | 1800 | re-list GCS this often |
| `BRIDGE_STATIONS` | (all 33) | comma-separated subset |
| `BRIDGE_REGISTRY_TABLE` | `argos-487318.argos.sensor_locations` | BQ fallback table |
| `BRIDGE_REGISTRY_SR_COL` | `sensor` | column name holding the station id |
| `BRIDGE_SNAP_TO_REGISTRY` | false | when a sidecar GPS is >50 km from the registry position, snap to registry instead of using the (warned) sidecar value |
| `BRIDGE_REGISTRY_REQUIRED` | false | refuse to start if the BQ registry load fails (default: log + continue) |

## Per-clip GPS

Location is resolved **per clip**, not per station. Order:

1. **Sidecar JSON next to the .wav** — `location.{latitude, longitude, altitude}`.
   When present, this becomes the clip's location.
2. **BigQuery `argos-487318.argos.sensor_locations`** — one query at
   startup, cached. Used as fallback when the sidecar has no location.

The handshake carries the first clip's resolved location. Subsequent
clips whose location differs emit a `LocationUpdate` before the
`AudioFrame`, so the stream reflects per-clip movement.

Sanity guard: when a sidecar GPS is >50 km from the station's
registry position, the bridge emits a `WARNING` and uses the sidecar
value anyway. Set `BRIDGE_SNAP_TO_REGISTRY=true` to flip that to
snap-to-registry behavior. (Use case: a sidecar with a placeholder
`21.29,-157.84` Honolulu reading should be ignored.)

`DeviceLocation.provider` is `"sidecar-gps"` or `"registry"` per
source. `status` is `LOCATION_STATUS_CURRENT` for sidecar GPS and
`LOCATION_STATUS_MANUAL` for registry fallback.

## Auth posture (TEST-only)

`GATEWAY_REQUIRE_AUTH` is off in argosuat. The bridge sends `device_id`
+ `auth_token_id="uat-test-<STATION>"` in the handshake but does not
sign a JWT. Once auth is flipped on, the bridge will need a small
addition: load the per-station private key from Secret Manager, sign
a short-lived JWT with `kid=<STATION>`, and send it as
`Authorization: Bearer <jwt>` gRPC metadata. The public keys are
already enrolled, so the gateway's verifier path will Just Work.

## Topology

```
[prod argos GCS] --read-only--> [argos-bridge VM, argosuat] --gRPC--> [gateway]
                                                                          |
                                                                          v
                                                                  [Redis stream]
                                                                          |
                                                                          v
                                                                    [inference]
                                                                          |
                                                                          v
                                                                    [admin UI]
```

Nothing in this directory writes to prod argos. The cross-project read
is the only prod touchpoint.
