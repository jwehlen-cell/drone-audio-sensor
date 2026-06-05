# audio_receiver — cross-project AUDIO egress measurement

Cloud Run gRPC service in **drone-audio-sensor** (us-west2). Each
simulated phone in argosuat opens a second persistent gRPC stream
to this service in parallel with its real stream to the gateway.
Frames are dropped immediately after the receiver counts byte
volumes — nothing about the audio is retained.

```
sim VM (argosuat)                          drone-audio-sensor
+----------------------+                   +-------------------------+
| replay_fleet         | -- gRPC stream -> | gateway (production)    |
| per simulated phone  |                   +-------------------------+
|                      |
|                      | -- gRPC stream -> +-------------------------+
|                      |    (test-only)    | audio_receiver          |
|                      |                   |   - DroneAudioStream    |
|                      |                   |   - AudioEgressStats    |
+----------------------+                   +-------------------------+
                                                  |
                                                  v
                                              Cloud Logging (60 s stats)
```

## What the receiver tracks

| Counter | Meaning |
|---|---|
| `test_run_tag` | Free-form label set by `ResetTestRun` |
| `handshakes_received` | Count of `ConnectHandshake` messages |
| `frames_received` | Count of `AudioFrame` messages |
| `wire_bytes` | Sum of every `ClientStreamMessage.ByteSize()` |
| `audio_payload_bytes` | Sum of `len(audio_frame.pcm16_mono)` (codec-encoded audio bytes) |
| `pcm_equivalent_bytes` | What the same audio would weigh as raw 16-bit mono PCM, derived from handshake `sample_rate_hz` + `frame_duration_ms`. **No FLAC decoding** — pure arithmetic, so the receiver burns no CPU on codec work. |
| `frames_pcm16` / `frames_wav` / `frames_flac` / `frames_unknown_codec` | Per-codec breakdown |
| `max_single_frame_bytes` | Peak single-message size |
| `stream_errors` | Stream-level RPC errors |
| `started_at_unix_ms` / `last_frame_at_unix_ms` | Wall-clock bookends |

No audio payload is persisted. The protobuf field is read for
`len()`, then the message object is released as the loop iterates.

## Compression ratio for free

The receiver computes `audio_payload_bytes` (the FLAC-encoded
on-wire audio bytes) AND `pcm_equivalent_bytes` (what that audio
would have weighed uncompressed). Compression ratio is then:

```
compression = 1 - (audio_payload_bytes / pcm_equivalent_bytes)
```

For a FLAC-encoded 5-second mono 16 kHz frame:
- PCM equivalent: 16,000 × 5 × 2 = 160,000 B
- FLAC actual: ~40–80,000 B (~50–75 % savings)

## Build + deploy

```bash
SHA=$(git rev-parse --short HEAD)
gcloud builds submit \
  --project=drone-audio-sensor \
  --config=cloudbuild/audio_receiver.yaml \
  --substitutions=_TAG=$SHA,_DEPLOY=true \
  .

URL=$(gcloud run services describe audio-receiver \
  --project=drone-audio-sensor --region=us-west2 \
  --format='value(status.url)')
HOST=${URL#https://}
echo "LOAD_TEST_AUDIO_EGRESS_TARGET=${HOST}:443"
echo "LOAD_TEST_AUDIO_EGRESS_AUDIENCE=${URL}"
```

## Cross-project IAM

The **sim VM's** SA in argosuat (the default compute SA, attached
during the test) needs `roles/run.invoker` on this service:

```bash
SIM_VM_SA=$(gcloud compute instances describe drone-sensor-dev-sim \
  --project=argosuat --zone=us-west2-a \
  --format='value(serviceAccounts[0].email)')

gcloud run services add-iam-policy-binding audio-receiver \
  --project=drone-audio-sensor --region=us-west2 \
  --member="serviceAccount:${SIM_VM_SA}" \
  --role=roles/run.invoker
```

## Wire it into a test

Set the audio-egress metadata on the sim VM in addition to the
existing `LOAD_TEST_*` knobs:

```bash
gcloud compute instances add-metadata drone-sensor-dev-sim \
  --project=argosuat --zone=us-west2-a \
  --metadata="LOAD_TEST_AUDIO_EGRESS_TARGET=${HOST}:443,LOAD_TEST_AUDIO_EGRESS_AUDIENCE=${URL}"

gcloud compute instances reset drone-sensor-dev-sim \
  --project=argosuat --zone=us-west2-a
```

The startup script picks up the two new vars and threads
`--audio-egress-target` + `--audio-egress-audience` into
`replay_fleet.py` automatically.

Tag a fresh run before the cadence starts producing data:

```bash
TOKEN=$(gcloud auth print-identity-token --audiences=${URL})
grpcurl -H "authorization: Bearer $TOKEN" \
  -d '{"test_run_tag":"audio-egress-2026-06-05"}' \
  ${HOST}:443 drone.audio_egress.AudioEgressStats/ResetTestRun
```

## Production safety

- The audio-egress tap is **off by default** (sim VM ships with no
  `LOAD_TEST_AUDIO_EGRESS_*` metadata).
- If the cross-project leg gets wedged or the receiver is down, the
  simulator's gateway stream is **not affected** — egress frames
  are dropped at the queue layer (zero-timeout put_nowait), but the
  gateway leg keeps publishing at full cadence.
- The audio receiver runs in a separate project on separate billing.
  Tear it down (`gcloud run services delete audio-receiver`) when
  the test is done.
