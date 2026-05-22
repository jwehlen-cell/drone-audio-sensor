# TAK Publisher

Subscribes to the drone-detections Pub/Sub topic, converts each event into a Cursor-on-Target XML payload, and streams it to a configured TAK Server over a persistent TCP/TLS connection.

## Pipeline

```
inference worker ---(JSON detection)---> Pub/Sub topic
                                              |
                                              v
                              pull subscription (this service)
                                              v
                          dedup by detection_id (LRU, in-memory)
                                              v
                              JSON -> Cursor-on-Target XML
                                              v
                       persistent TLS socket -> TAK Server :8089
                                              v
                            ATAK / WinTAK clients see the marker
```

Pub/Sub message ack is deferred until the TAK Server write succeeds; on
failure the message is `nack`ed so Pub/Sub redelivers (eventually to the
DLQ once `max_delivery_attempts` is exceeded).

## TAK Server credential format

The publisher loads credentials from a Secret Manager secret (path in
`TAK_PUBLISHER_TAK_CREDENTIALS_SECRET`). Expected JSON payload:

```json
{
  "host": "tak.example.mil",
  "port": 8089,
  "use_tls": true,
  "client_cert_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
  "client_key_pem":  "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "ca_cert_pem":     "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"
}
```

For testing with an unauthenticated TAK Server, set `use_tls=false` and
leave the cert fields empty.

## Configuration

All env vars are `TAK_PUBLISHER_`-prefixed:

| Variable | Default | Notes |
|---|---|---|
| `TAK_PUBLISHER_DETECTIONS_SUBSCRIPTION` | (required) | `projects/.../subscriptions/...` |
| `TAK_PUBLISHER_TAK_CREDENTIALS_SECRET` | empty | Secret Manager resource id |
| `TAK_PUBLISHER_TAK_DEFAULT_HOST` | empty | Used if secret is missing or host blank |
| `TAK_PUBLISHER_TAK_DEFAULT_PORT` | `8089` | |
| `TAK_PUBLISHER_TAK_USE_TLS` | `true` | |
| `TAK_PUBLISHER_COT_EVENT_TYPE` | `a-u-A` | CoT type string |
| `TAK_PUBLISHER_COT_STALE_SECONDS` | `180` | Marker lifetime on TAK display |
| `TAK_PUBLISHER_COT_UID_PREFIX` | `drone-detection` | Used to build the CoT `uid` |
| `TAK_PUBLISHER_DEDUP_WINDOW_SECONDS` | `120` | LRU dedup window (informational; size driven by cache_size) |
| `TAK_PUBLISHER_DEDUP_CACHE_SIZE` | `1024` | In-memory LRU size |
| `TAK_PUBLISHER_HEALTH_PORT` | `8080` | Cloud Run health probe port |
| `TAK_PUBLISHER_CLOUD_LOGGING` | `false` | |

## CoT mapping

| CoT field | Source |
|---|---|
| `uid` | `<cot_uid_prefix>.<detection_id>` |
| `type` | `cot_event_type` |
| `time`, `start` | `last_frame_timestamp_ms` (or now) |
| `stale` | `time + cot_stale_seconds` |
| `point.lat`, `point.lon` | `device_location.latitude`, `device_location.longitude` |
| `point.hae` | `device_location.altitude_m` if present |
| `point.ce` | `device_location.accuracy_m` if present |
| `contact.callsign` | `device_id` |
| `remarks` | Drone score summary + model + site label |

Detections **without** a usable location are skipped (acked, not sent) — without a point on the map there's nothing useful to display.

## Local development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=src
export TAK_PUBLISHER_DETECTIONS_SUBSCRIPTION=projects/your-proj/subscriptions/drone-sensor-dev-tak
export TAK_PUBLISHER_TAK_DEFAULT_HOST=127.0.0.1
export TAK_PUBLISHER_TAK_DEFAULT_PORT=8087
export TAK_PUBLISHER_TAK_USE_TLS=false
python -m tak_publisher.main
```

For a fast end-to-end test without a real TAK Server, run a netcat listener:

```
nc -l 8087   # then publish a test detection to your Pub/Sub topic
```

## Container build

```
docker build -f backend/tak_publisher/Dockerfile -t drone-tak-publisher:dev .
```
