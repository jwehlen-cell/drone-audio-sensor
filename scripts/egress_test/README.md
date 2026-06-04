# Egress-cost test runbook

Measures GCP egress bytes under best-effort encoding (typed protobuf
batches, gzip on the wire, persistent HTTP/2 client). The cloud
publisher consumes detections from the prod Pub/Sub topic and POSTs
batched payloads to a receiver on the test operator's laptop; the
receiver drops the data and counts bytes.

```
detections (Pub/Sub JSON, ~1.5 KB)
        |
        v
+-------------------------------+
|  drone-sensor-dev-egress-     |   Cloud Run, us-west2
|  publisher                    |
|                               |
|  JSON -> Detection pb         |
|  EgressBatch (N items)        |
|  gzip                         |
|  HTTPS POST (HTTP/2 keep-alive)|
+-------------------------------+
        |
        v   (egress out of GCP -- this is what we're measuring)
        |
+-------------------------------+
|  ngrok / direct-tunnel        |
+-------------------------------+
        |
        v
+-------------------------------+
|  receiver.py on laptop        |
|  - reads body, drops it       |
|  - logs wire bytes (the cost) |
|  - decompresses & logs unc    |
+-------------------------------+
```

## What changes for this test (vs the 1,000-phone test)

| Knob | 1,000-phone test | Egress-cost test |
|---|---|---|
| Cadence | 30 s | **5 s** (6x more inflight stream cycles) |
| Detection sink | Pub/Sub -> TAK publisher | Pub/Sub -> **egress publisher** (new) |
| Receiver | TAK server VM | **laptop receiver** (drops data) |
| Wire format | CoT XML (verbose) | **protobuf + gzip** (densest reasonable) |

Set `LOAD_TEST_CADENCE_SECONDS=5` on the sim VM metadata to switch
cadence. Everything else (bases, phones-per-base, source clip) is
unchanged.

## 1. Operator-side setup (laptop)

```bash
# Start the receiver. Drops every payload, prints stats every 10 s.
python3 scripts/egress_test/receiver.py --port 8080
```

Expose to the internet. `ngrok` is the cheapest path:

```bash
ngrok http 8080
# -> grab the https://<token>.ngrok.io URL it prints
```

Note: ngrok is in the wire path. It does *not* re-compress HTTP
bodies, so the byte count at the receiver equals the byte count
that left GCP. The TLS frame overhead is small but non-zero; the
publisher-side `wire_bytes` stat is the authoritative number for
billing math.

## 2. GCP-side setup (one-time, per-deploy)

Create a Pub/Sub subscription that the egress publisher reads from.
It's a parallel subscription on the existing detections topic, so
the TAK publisher keeps getting events too -- nothing about the
prod path changes.

```bash
TOPIC=projects/argosuat/topics/<existing-detections-topic-from-inference-config>

gcloud pubsub subscriptions create aftac-argosuat-detections-egress \
  --project=argosuat \
  --topic=$TOPIC \
  --ack-deadline=30
```

Build + deploy the publisher:

```bash
gcloud builds submit \
  --project=argosuat \
  --config=cloudbuild/egress_publisher.yaml \
  --substitutions=_TAG=$(git rev-parse --short HEAD) \
  .
```

Configure target URL + subscription on the Cloud Run service:

```bash
NGROK_URL=https://<token>.ngrok.io/egress

gcloud run services update drone-sensor-dev-egress-publisher \
  --project=argosuat --region=us-west2 \
  --set-env-vars="\
EGRESS_DETECTIONS_SUBSCRIPTION=projects/argosuat/subscriptions/aftac-argosuat-detections-egress,\
EGRESS_TARGET_URL=$NGROK_URL,\
EGRESS_BATCH_SIZE=100,\
EGRESS_BATCH_TIMEOUT_S=5.0,\
EGRESS_COMPRESSION=gzip"
```

(Optional) Add an auth header so the publisher's traffic can be
distinguished from random ngrok pokes:

```bash
gcloud run services update drone-sensor-dev-egress-publisher \
  --project=argosuat --region=us-west2 \
  --update-env-vars="EGRESS_AUTH_HEADER=Bearer egress-test-$(openssl rand -hex 8)"
```

## 3. Kick off the test

Configure the sim VM the same way as the 1,000-phone test but with
`LOAD_TEST_CADENCE_SECONDS=5` instead of 30:

```bash
gcloud compute instances add-metadata drone-sensor-dev-sim \
  --project=argosuat --zone=us-west2-a \
  --metadata="^@^LOAD_TEST_MODE=true@LOAD_TEST_BASES=Langley,Vandenberg,Nellis,Hickam,WrightPatterson,Eielson,Andersen,Kadena,Ramstein,Buckley@LOAD_TEST_PHONES_PER_BASE=100@LOAD_TEST_CADENCE_SECONDS=5@LOAD_TEST_CODEC=flac@LOAD_TEST_CLIP_GCS=...@LOAD_TEST_GROUND_TRUTH_GCS=..."

gcloud compute instances reset drone-sensor-dev-sim \
  --project=argosuat --zone=us-west2-a
```

Watch the receiver's rolling stats line. The publisher also exposes
`/stats` on its Cloud Run health port if you want the GCP-side
view.

## 4. Reading the numbers

```
=== egress receiver rolling (3600.0s elapsed) ===
  requests:                    14,500
  wire bytes total:           18.42 MiB  (19,317,235 bytes)
  uncompressed total:         85.13 MiB  (89,267,521 bytes)
  avg per request:       1.30 KiB wire / 6.02 KiB unc
  gzip compression:      78.4%
  sustained wire rate:   5.36 KiB/s
  sustained unc rate:    24.78 KiB/s
  max single request:    2.18 KiB
```

* `wire bytes total` is the egress-billable number. GCP charges
  egress per GiB (rate depends on destination); multiply by the
  applicable rate.
* `uncompressed total` is what the receiver would have processed
  without gzip -- a reasonable upper bound for "naive" wire cost.
* The `(unc - wire) / unc` ratio at the bottom of the report is the
  effective savings from this encoding pipeline.

## 5. Teardown

Stop the receiver (Ctrl-C; it prints a FINAL block). Then:

```bash
# Pause the egress publisher between tests so it doesn't bill while
# idle. Cloud Run does scale to 0 with no traffic, but explicit
# stop is cleaner if you'll be away for hours.
gcloud run services update drone-sensor-dev-egress-publisher \
  --project=argosuat --region=us-west2 \
  --min-instances=0 --max-instances=0
```

Restore the sim VM cadence to 30 s (or whatever the next test wants)
by re-resetting with the desired metadata.

## What this *doesn't* measure

* TLS/HTTP framing overhead on the wire is included in
  `wire bytes`, so the number reflects what you pay GCP. It is *not*
  the protobuf-only payload size.
* ngrok adds a separate ingress charge to the laptop (free tier
  covers it for short tests). It does not double-count GCP egress.
* The TAK-publisher path is untouched, so this measures the cost
  of a *parallel* egress sink, not a swap.
