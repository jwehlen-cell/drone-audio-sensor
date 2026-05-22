# Operator Runbook

Day-to-day operations for the drone audio sensor pipeline.

## Architecture at a glance

```
Phone (Android, dedicated device)
   |  EC P-256 JWT in Authorization header
   v
Cloud Run gateway (us-central1)
   |  XADD audio_frames
   v
Memorystore Redis (1 GB, BASIC)
   |  XREADGROUP "inference"
   v
Cloud Run inference workers (YAMNet)
   |  JSON detection event
   v
Pub/Sub topic "drone-sensor-<env>-detections" --> DLQ
   |
   v
Cloud Run TAK publisher
   |  CoT XML
   v
TAK Server (external, TCP/TLS :8089)
   |
   v
ATAK / WinTAK clients
```

## Common tasks

### Add a new device

See [PROVISIONING.md](PROVISIONING.md). TL;DR:

```bash
python scripts/provision_device.py register DRONE-SENSOR-042 \
    --pubkey device_042.pub.pem --site "Site B"
```

### Revoke a device

```bash
python scripts/provision_device.py revoke DRONE-SENSOR-042 \
    --reason "phone lost in storm"
```

Effective within `GATEWAY_JWT_PUBLIC_KEY_CACHE_SECONDS` (default 300s).

### List active devices

```bash
python scripts/provision_device.py list --site "Site B"
```

### Deploy a new gateway image

```bash
REPO=$(terraform -chdir=iac/terraform output -raw artifact_registry_repo)
TAG=$(git rev-parse --short HEAD)

docker build -f backend/gateway/Dockerfile -t $REPO/gateway:$TAG .
docker push $REPO/gateway:$TAG
gcloud run deploy drone-sensor-dev-gateway \
    --image=$REPO/gateway:$TAG --region=us-central1
```

`terraform apply` will not fight you — `image` is on `ignore_changes`.

Roll back: `gcloud run services update-traffic drone-sensor-dev-gateway --to-revisions=PREV_REVISION=100`.

### Deploy a new inference image

```bash
docker build -f backend/inference/Dockerfile -t $REPO/inference:$TAG .
docker push $REPO/inference:$TAG
gcloud run deploy drone-sensor-dev-inference \
    --image=$REPO/inference:$TAG --region=us-central1
```

### Deploy a new TAK publisher image

```bash
docker build -f backend/tak_publisher/Dockerfile -t $REPO/tak-publisher:$TAG .
docker push $REPO/tak-publisher:$TAG
gcloud run deploy drone-sensor-dev-tak-publisher \
    --image=$REPO/tak-publisher:$TAG --region=us-central1
```

### Update TAK Server credentials

```bash
gcloud secrets versions add drone-sensor-dev-tak-credentials \
    --data-file=tak_creds.json

# Restart the publisher so it loads the new version
gcloud run services update drone-sensor-dev-tak-publisher \
    --update-env-vars=_FORCE_RELOAD=$(date +%s) \
    --region=us-central1
```

### Scale up / down

Each Cloud Run service has `min_instances` and `max_instances` Terraform variables. To bump the gateway from 1→3 minimum:

```bash
# edit iac/terraform/terraform.tfvars
gateway_min_instances = 3

terraform -chdir=iac/terraform apply
```

## Investigation playbook

### "Device shows online but no audio frames arriving"

1. Confirm the phone status app shows STREAMING (green).
2. Check the gateway logs for that device:
   ```bash
   gcloud run services logs read drone-sensor-dev-gateway \
       --region=us-central1 --limit=50 \
       --format='value(textPayload)' \
       | grep DRONE-SENSOR-042
   ```
3. Common causes:
   - **`UNAUTHENTICATED`** — public key not registered, JWT audience mismatch, or device revoked. Check Firestore via `provision_device.py info`.
   - **`PERMISSION_DENIED: JWT subject does not match handshake device_id`** — the phone's stored `device_id` doesn't match the registered key. Re-register with the correct ID or have the phone regenerate via SharedPreferences clear.
   - **Connect succeeds, no frames** — phone may lack `RECORD_AUDIO` permission. Check the phone's UI for the permission prompt.

### "Frames arriving but no detections firing"

1. Check inference logs:
   ```bash
   gcloud run services logs read drone-sensor-dev-inference \
       --region=us-central1 --limit=100 \
       --format='value(textPayload)'
   ```
2. Look for `worker_ready` and `frame_handler_failed` lines.
3. If everything looks healthy but no detection: the audio simply doesn't have YAMNet's `Drone` class above threshold. Lower `INFERENCE_DETECTION_THRESHOLD` temporarily for testing:
   ```bash
   gcloud run services update drone-sensor-dev-inference \
       --update-env-vars=INFERENCE_DETECTION_THRESHOLD=0.2 \
       --region=us-central1
   ```
4. Validate with a known drone audio clip via direct phone playback.

### "Detections firing but not appearing in TAK"

1. Check the TAK publisher logs:
   ```bash
   gcloud run services logs read drone-sensor-dev-tak-publisher \
       --region=us-central1 --limit=100
   ```
2. Common causes:
   - **`tak_connect_failed`** — wrong host/port or unreachable. Verify by `nc -zv $TAK_HOST $TAK_PORT` from a Cloud Shell.
   - **TLS handshake failure** — cert mismatch, expired CA. Validate the secret payload JSON.
   - **`detection_skipped_no_location`** — device has no GPS fix. Phones need outdoor placement for first GPS lock; check `Firestore: devices/{id}.current_location`.
3. Check the DLQ:
   ```bash
   gcloud pubsub subscriptions create dlq-debug --topic=drone-sensor-dev-detections-dlq
   gcloud pubsub subscriptions pull dlq-debug --auto-ack --limit=10
   ```

### "Memorystore filling up"

```bash
gcloud redis instances describe drone-sensor-dev-redis --region=us-central1 \
    --format='value(memorySizeGb,reservedIpRange,redisVersion)'
```

If `redis.usage_ratio > 0.75`, either:
- Reduce `GATEWAY_FRAME_STREAM_MAXLEN` (default 3000) on the gateway env.
- Reduce `GATEWAY_REDIS_TTL_SECONDS` (default 300).
- Upgrade tier: `redis_tier = "STANDARD_HA"` + larger `redis_memory_gb`.

### "Pub/Sub backlog growing"

```bash
gcloud pubsub subscriptions describe drone-sensor-dev-tak-publisher \
    --format='value(numUndeliveredMessages)'
```

If non-trivial: TAK publisher is the bottleneck. Scale up `tak_publisher_max_instances`, or investigate TAK Server response time.

### "DLQ has messages"

Pull and inspect:

```bash
gcloud pubsub subscriptions pull dlq-debug --auto-ack --limit=10 \
    --format='value(message.data)' | base64 -d | jq .
```

Common: malformed detection events from an experimental inference build, or a TAK Server hostname change that's making every publish fail.

## Disaster recovery

| Scenario | Recovery |
|---|---|
| Cloud Run service deleted | `terraform apply` recreates it; image is `ignore_changes` so re-deploy via `gcloud run deploy` |
| Memorystore down / corrupted | Per-device state lost (TTL 5min); restart workers; new sessions repopulate naturally |
| Firestore device collection deleted | Re-register all devices from your backup CSV / from each phone's exported public key. Without this the gateway can't authenticate. **Keep a backup of the devices collection.** |
| Pub/Sub topic deleted | `terraform apply` recreates; the inference workers will silently fail to publish. Restart inference. |
| TAK Server unreachable | Pub/Sub holds messages for `message_retention_duration` (1 day default); set higher if you expect long outages |
| Wrong image deployed (regression) | `gcloud run services update-traffic <svc> --to-revisions=<previous>=100` |

## Cost watch

Cost driver: Cloud Run inference vCPU-seconds.

```bash
gcloud billing accounts list
# Pivot the linked billing project's reports in the GCP console
```

Set a budget alert in the GCP billing console at ~70% of your monthly plan; the Terraform here does not provision billing alerts (cross-project resources, separate IAM model).

For a rough estimate of inference cost: `vCPU-hours × $0.0595 + memory-GB-hours × $0.00842`.

## Backups

| What | How |
|---|---|
| Firestore `devices` collection | `gcloud firestore export gs://<bucket>/devices/$(date +%Y%m%d) --collection-ids=devices` weekly |
| Terraform state | Already in GCS with versioning if you've configured a backend; otherwise commit `terraform.tfstate.backup` |
| TAK credentials | Secret Manager retains all versions by default — no separate backup needed |
| Pub/Sub messages | Pub/Sub retains for `message_retention_duration` (currently 1 day); for longer history, attach a separate retention subscription to BigQuery |

## On-call decision tree

```
Are devices reporting "online" in the dashboard?
├── No  → Gateway issue. Check Cloud Run logs + Redis health.
└── Yes →
    Are detections being published to Pub/Sub?
    ├── No  → Inference issue. Check inference logs + Redis stream depth.
    └── Yes →
        Are CoT events reaching TAK?
        ├── No  → TAK publisher issue. Check publisher logs + TAK Server connectivity + cert validity.
        └── Yes → ✅ Pipeline healthy. Investigate device-side issue (mic, network, power).
```

## Useful queries

```bash
# Frame ingest rate (last 5 min)
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="drone-sensor-dev-gateway" AND jsonPayload.event="handshake_received"' \
    --limit=10 --format='value(timestamp,jsonPayload.device_id)'

# Detections published (last hour)
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="drone-sensor-dev-inference" AND jsonPayload.event="detection_published"' \
    --limit=20

# CoT publishes (last hour)
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="drone-sensor-dev-tak-publisher" AND jsonPayload.event="cot_published"' \
    --limit=20
```
