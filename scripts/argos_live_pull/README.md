# Argos live-pull subscriber

Subscribes to the prod Argos Pub/Sub notification topic, authenticates
as the cross-project SA the Argos team minted for us
(`argos-bridge@argos-487318`), pulls each `.wav` + `.json` sidecar as
they land in `gs://aftac-argos-dataflow-unzipped`, and streams them
into the argosuat gateway under our existing `SIM-SHAW-SH###` device
ids. Synthesizes plausible SOH (battery, temp, voltage, cellular RSSI)
per station so the admin dashboard renders full rows even though the
real Argos sensors don't emit those fields.

This is the **live tail** path. The earlier `scripts/argos_bridge/`
service is **historical replay** off a GCS prefix walk and is
authentically a different thing — they can run side-by-side or you
can retire the bridge once live-pull is healthy.

## Prereqs

### 1. Argos side (do these in the Argos team's project, `argos-487318`)

The SA already exists: `argos-bridge@argos-487318.iam.gserviceaccount.com`.
Generate a JSON key for it:

* Console: <https://console.cloud.google.com/iam-admin/serviceaccounts/details/109037786253246493417/keys?project=argos-487318>
* `Add Key → Create new key → JSON → Create`. Browser downloads a
  `argos-bridge-XXXX.json` file. **Treat this like a password.**

The SA also needs two roles on the Argos side. Ask the Argos team to
grant them on the topic + bucket (do NOT grant project-wide):

```bash
# pubsub: subscribe to the live notification topic
gcloud pubsub topics add-iam-policy-binding aftac-argos-unzipped \
  --project=argos-487318 \
  --member=serviceAccount:argos-bridge@argos-487318.iam.gserviceaccount.com \
  --role=roles/pubsub.subscriber

# storage: read clips out of the unzipped bucket
gcloud storage buckets add-iam-policy-binding \
  gs://aftac-argos-dataflow-unzipped \
  --project=argos-487318 \
  --member=serviceAccount:argos-bridge@argos-487318.iam.gserviceaccount.com \
  --role=roles/storage.objectViewer
```

A subscription on the topic is also needed. Either the Argos team
creates one with our SA as `--push-auth-service-account=...` (push
delivery, won't fit our pull-based subscriber), or **they let us pull
on a subscription named** `aftac-argos-unzipped-drone-sensor`:

```bash
gcloud pubsub subscriptions create aftac-argos-unzipped-drone-sensor \
  --project=argos-487318 \
  --topic=aftac-argos-unzipped \
  --ack-deadline=60
gcloud pubsub subscriptions add-iam-policy-binding aftac-argos-unzipped-drone-sensor \
  --project=argos-487318 \
  --member=serviceAccount:argos-bridge@argos-487318.iam.gserviceaccount.com \
  --role=roles/pubsub.subscriber
```

### 2. Argosuat side (we own this)

Stash the SA-key JSON as a Secret Manager secret so the subscriber can
read it at startup:

```bash
gcloud secrets create argos-live-pull-sa-key \
  --project=argosuat --replication-policy=automatic
gcloud secrets versions add argos-live-pull-sa-key \
  --project=argosuat \
  --data-file=/path/to/argos-bridge-XXXX.json
```

The host SA (the argosuat VM's SA, or your local `gcloud auth`
context) needs `secretmanager.secretAccessor` on the secret. The
`drone-sensor-dev-argos-bridge` SA already has that grant project-wide.

## Run

```bash
.venv/bin/python scripts/argos_live_pull/subscriber.py
```

Or with a local key file (bench testing):

```bash
.venv/bin/python scripts/argos_live_pull/subscriber.py \
  --sa-key-path /path/to/argos-bridge-XXXX.json
```

Log emits a stats line each minute with `received / forwarded /
failed` counts. Each clip causes an open-stream / handshake / 1 audio
frame / close cycle into our argosuat gateway, same shape as the
replay-fleet phones.

## Tunables (env vars)

| var | default | meaning |
|---|---|---|
| `ARGOS_LIVE_TOPIC` | `projects/argos-487318/topics/aftac-argos-unzipped` | source topic (info only — we subscribe to a subscription) |
| `ARGOS_LIVE_SUBSCRIPTION` | `projects/argos-487318/subscriptions/aftac-argos-unzipped-drone-sensor` | the pull subscription we own |
| `ARGOS_LIVE_BUCKET` | `aftac-argos-dataflow-unzipped` | source bucket |
| `ARGOS_LIVE_SA_KEY_SECRET` | `projects/argosuat/secrets/argos-live-pull-sa-key/versions/latest` | SA key location |
| `BRIDGE_GATEWAY_URL` | argosuat gateway run URL | streaming target |
| `BRIDGE_GATEWAY_TLS` | true | argosuat is HTTPS-fronted |
| `BRIDGE_CODEC` | `pcm16` | `pcm16` or `flac` |
| `ARGOS_LIVE_MAX_INFLIGHT` | 8 | concurrent clip forwards |

## SOH synthesis

Argos sensors don't emit battery/network telemetry the way our phones
do. Rather than leave the dashboard cells blank, the subscriber
generates per-station SOH at handshake time:

* Battery 70–95% baseline, drifts 1% per ~5 min wall-clock
* Battery temperature 22–38°C baseline, drifts ±2°C/hour
* Battery voltage 3.7–4.15 V, jitters ±5 mV
* Cellular RSSI -70 to -105 dBm baseline, drifts ±2 dB/min
* Battery health = GOOD (constant)
* Network = CELLULAR_LTE

The values are **synthetic** but consistent per-station, so an
operator who reads them as "this is what the sensor says" is making
the right read for a UAT environment — they're a stand-in until the
prod Argos sensors actually wire SOH.

## Deployment shape

For the first run, just run on a laptop with the SA key file. Once
healthy, move to the existing argos-bridge VM (or a new sibling) —
same systemd-unit pattern as `scripts/argos_bridge/startup.sh`. The
script has no listening socket; it's pure egress to Pub/Sub + Storage
and gRPC to our gateway.

## Why a different subscriber from `argos_bridge`

| | `argos_bridge` | `argos_live_pull` |
|---|---|---|
| Mode | pull from GCS in chronological order | subscribe to live Pub/Sub notifications |
| Auth | argosuat SA needs cross-project IAM (not done) | argosuat reads SA key for argos-side SA |
| Use case | replay historical clips for end-to-end testing | feed live argos data into the pipeline |
| Pacing | real-time per-station | as fast as messages arrive |
