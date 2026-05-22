# Status + Admin Dashboard

A small FastAPI service (`backend/admin/`) provides a status page and a
registered-phones admin page over the same Firestore + Redis data the
gateway and inference workers already use. There is no new database, no
new always-on worker, and no static frontend build — server-rendered
Jinja2 templates ship in the container.

## Pages and endpoints

| URL | Purpose |
|---|---|
| `/` | **Status page.** Live device states pulled from Redis (`device:{id}` keys), joined to each device's Firestore registration. Below that, the last hour of detection events from the `detections` collection. |
| `/registered` | **Registered phones page.** Every device document, with per-row buttons that drive lifecycle transitions (`active` / `lost` / `revoked` / `wipe_requested`). |
| `/api/connected` | JSON of currently-live device states (Redis SCAN). |
| `/api/registered` | JSON of every registered device with state, site, last-seen, key fingerprint, latest location. Never returns the full PEM. |
| `/api/detections/recent` | JSON of the last hour of detections. |
| `/api/devices/{id}/state` | `POST` (form-encoded) — change a device's lifecycle state. Validates the transition against the rules in [DEVICE_LIFECYCLE.md](DEVICE_LIFECYCLE.md). |

## Architecture

```
[admin user]
   |  HTTPS w/ IAM-validated identity token
   v
Cloud Run admin service  (scale to zero, min_instances = 0)
   |               \
   v                v
Firestore         Memorystore Redis
 - devices         - device:{id} (TTL ~5 min)
 - detections (TTL 1 h)
```

The gateway writes lifecycle state to Firestore. The inference worker
writes each confirmed detection to Firestore (`detections/{id}`) with an
`expires_at` field; a Firestore TTL policy
(`google_firestore_field.detections_ttl`) deletes those docs after one
hour, so the dashboard's "last hour" query stays cheap and storage
stays bounded.

## Data model

Existing `devices/{device_id}` documents now use a `state` field for
lifecycle and reserve `status`-derived behavior for live connection
freshness (computed from `last_seen_ms`, not stored as a field). State
values:

```
active | lost | revoked | wipe_requested | wipe_sent
```

See [DEVICE_LIFECYCLE.md](DEVICE_LIFECYCLE.md) for the full state model
and transition matrix.

New `detections/{detection_id}` documents look like:

```jsonc
{
  "detection_id": "abc123…",
  "device_id": "DRONE-SENSOR-001",
  "site_label": "Site A",
  "average_score": 0.72,
  "peak_score": 0.91,
  "last_frame_timestamp_ms": 1735692000000,
  "published_at_ms": 1735692001234,
  "device_location": { "latitude": 35.1, "longitude": -78.4 },
  "model": { "name": "yamnet", "version": "1" },
  "expires_at": "<Timestamp now+1h>"
}
```

## Cost shape

- Admin Cloud Run service is **scale-to-zero** (`min_instances = 0`).
  No idle cost; one warm instance spins up when an admin loads a page.
- Detection docs: Firestore TTL set on `expires_at` (1 hour by default)
  auto-deletes old documents, so the recent-detections query reads at
  most ~hourly volume and storage is bounded.
- Heartbeats and per-frame device state stay in Redis — we explicitly
  do *not* write every heartbeat to Firestore.

## Auth

Cloud Run service is created with **no public invoker grant**.
`admin_invoker_members` (Terraform variable) holds the list of
principals (`user:…`, `group:…`, `serviceAccount:…`) that get
`roles/run.invoker`. For browser access:

```bash
gcloud run services proxy drone-sensor-dev-admin --region=us-central1
# open http://localhost:8080
```

The proxy injects an ID token signed by your gcloud identity; Cloud Run
validates it and forwards the request with
`X-Goog-Authenticated-User-Email` set, which the admin app uses to
identify the caller in audit logs.

**TODO:** front the service with IAP (Identity-Aware Proxy) for a
browser-friendly experience without the gcloud proxy.

## Cloud Monitoring dashboard

In addition to the in-app admin UI, Terraform provisions a Cloud
Monitoring dashboard (see `iac/terraform/monitoring.tf` →
`google_monitoring_dashboard.drone_sensor`) for backend health. Six
tiles cover gateway requests + latency + instance counts, Memorystore
memory, Pub/Sub publish/ack rates, and TAK backlog. The paired alert
policies are documented in [RUNBOOK.md](RUNBOOK.md).
