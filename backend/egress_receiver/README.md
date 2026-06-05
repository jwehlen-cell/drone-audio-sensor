# egress_receiver — cross-project egress measurement

Cloud Run gRPC service in **drone-audio-sensor** (us-west2). Receives
batched detection events from the `egress_publisher` in **argosuat**
over a long-lived gRPC bidi stream, drops every payload, and keeps
per-test-run accumulators only (no raw data is persisted).

```
argosuat                                  drone-audio-sensor
+----------------------+                  +-------------------------+
| egress_publisher     |                  | egress_receiver         |
| (Cloud Run, gRPC     | -- gRPC bidi --> | (Cloud Run, gRPC server)|
|  client)             |   (StreamBatches)|                         |
|                      |  <-- BatchAck -- |                         |
+----------------------+                  +-------------------------+
        ^                                          |
        | reads Pub/Sub                            | logs stats
        |   detections                             | every 60 s
        |                                          v
        |                                       Cloud Logging
```

## Why this shape

- **Cost separation.** argosuat is on the AFTAC billing account;
  drone-audio-sensor is on Joseph Wehlen's personal billing. Every
  byte that lands at this receiver is one billable boundary crossing
  away from production, and the two halves show up on different
  invoices.
- **Realistic egress measurement.** Cross-project + cross-region
  traffic is billed as egress at the standard inter-region rate.
  Bytes sent here = bytes that would egress to a real off-GCP
  consumer (modulo rate differences).
- **Test-only.** The receiver is deployed only when an egress test
  is in progress. Tear down + redeploy at the next test, so the
  service spends ~no time on the bill between runs.

## Stats kept

Per active test run, in memory only:

| Field | Meaning |
|---|---|
| `test_run_tag` | Free-form label set via `ResetTestRun` |
| `batches_received` | Count of `EgressBatch` messages seen |
| `detections_received` | Sum of `len(batch.detections)` across batches |
| `wire_bytes_received` | gRPC-unframed batch payload bytes |
| `proto_bytes_received` | Sum of `EgressBatch.ByteSize()` |
| `max_single_batch_bytes` | Largest one-batch payload |
| `errors` | Stream errors |
| `started_at_unix_ms` / `last_batch_at_unix_ms` | Wall-clock bookends |

The receiver never persists the contents of an `EgressBatch`. Acks
flow back to the publisher with the batch's wire byte count for
cross-checking; the publisher then advances its Pub/Sub ack.

## Build + deploy

```bash
SHA=$(git rev-parse --short HEAD)
gcloud builds submit \
  --project=drone-audio-sensor \
  --config=cloudbuild/egress_receiver.yaml \
  --substitutions=_TAG=$SHA,_DEPLOY=true \
  .
```

After deploy, capture the service URL + gRPC target so the
publisher in argosuat can be pointed at it:

```bash
URL=$(gcloud run services describe egress-receiver \
  --project=drone-audio-sensor --region=us-west2 \
  --format='value(status.url)')
HOST=${URL#https://}
TARGET=${HOST}:443
echo "EGRESS_RECEIVER_TARGET=${TARGET}"
echo "EGRESS_RECEIVER_AUDIENCE=${URL}"
```

## Cross-project IAM

Run once after the receiver is first deployed (or after the
publisher's SA rotates):

```bash
# The argosuat publisher SA needs run.invoker on the receiver
# service in drone-audio-sensor.
PUBLISHER_SA=$(gcloud run services describe drone-sensor-dev-egress-publisher \
  --project=argosuat --region=us-west2 \
  --format='value(spec.template.spec.serviceAccountName)')

gcloud run services add-iam-policy-binding egress-receiver \
  --project=drone-audio-sensor --region=us-west2 \
  --member="serviceAccount:${PUBLISHER_SA}" \
  --role=roles/run.invoker
```

## Test runbook

1. Build + deploy the receiver (above).
2. Update the publisher in argosuat (deploy script in `cloudbuild/
   egress_publisher.yaml`).
3. Tag a fresh test run:
   ```bash
   grpcurl -d '{"test_run_tag":"egress-1h-5s-cadence-2026-06-05"}' \
     -H "authorization: Bearer $(gcloud auth print-identity-token \
       --audiences=${URL})" \
     ${TARGET} drone.egress.EgressReceiver/ResetTestRun
   ```
4. Run the load test.
5. Pull stats periodically:
   ```bash
   grpcurl -d '{}' -H "authorization: Bearer $TOKEN" \
     ${TARGET} drone.egress.EgressReceiver/GetStats
   ```
6. Tear down the receiver after the test:
   ```bash
   gcloud run services delete egress-receiver \
     --project=drone-audio-sensor --region=us-west2 --quiet
   ```

## Why not HTTP?

- HTTP POST per batch = 1 Cloud Run request per batch
- gRPC bidi = 1 Cloud Run request per stream lifetime (~1 per
  publisher instance)
- Persistent stream amortizes the TLS handshake CPU across the
  whole test run
- Native protobuf wire framing — no Content-Type, no
  Content-Encoding header overhead per message
- Matches the production phone-app shape

The earlier HTTP+gzip+ngrok path is in git history if you ever want
a comparison. This is the cheaper, more production-shaped target.
