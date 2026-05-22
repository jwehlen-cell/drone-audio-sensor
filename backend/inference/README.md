# Inference Worker

Consumes audio frames from the Redis Stream populated by the gateway, runs each 1-second clip through YAMNet via TF Hub, maintains a per-device score buffer, and publishes confirmed drone detections to a Pub/Sub topic.

## Pipeline

```
gateway --(XADD audio_frames)--> Redis Stream
                                      |
                       XREADGROUP via consumer group "inference"
                                      v
                              YAMNet (TF Hub yamnet/1)
                                      v
                       per-device score ring buffer (Redis LIST)
                                      v
                        K-of-N + average threshold check
                                      v
                       suppression window check (Redis key + TTL)
                                      v
                      JSON event -> Pub/Sub detections topic
```

## Detection logic

A detection fires when the **last N frames** include at least **K frames** scoring above `INFERENCE_DETECTION_THRESHOLD` for any of the configured drone classes (default: AudioSet class `Drone`). Once a detection fires for a device, further detections are suppressed for `INFERENCE_SUPPRESSION_WINDOW_SECONDS` (default: 60 s).

## Configuration

All env vars are `INFERENCE_`-prefixed:

| Variable | Default | Notes |
|---|---|---|
| `INFERENCE_REDIS_HOST` | `127.0.0.1` | Memorystore host |
| `INFERENCE_FRAME_STREAM_KEY` | `audio_frames` | Must match the gateway |
| `INFERENCE_CONSUMER_GROUP` | `inference` | Shared across all workers |
| `INFERENCE_READ_BATCH_SIZE` | `8` | Frames per `XREADGROUP` call |
| `INFERENCE_PUBSUB_DETECTIONS_TOPIC` | (required) | e.g. `projects/.../topics/drone-sensor-dev-detections` |
| `INFERENCE_DETECTION_THRESHOLD` | `0.5` | YAMNet drone-class probability |
| `INFERENCE_SCORE_BUFFER_SIZE` | `5` | Frames retained per device |
| `INFERENCE_MIN_FRAMES_OVER_THRESHOLD` | `3` | "K of N" trigger |
| `INFERENCE_SUPPRESSION_WINDOW_SECONDS` | `60` | Time to wait before re-alerting |
| `INFERENCE_HEALTH_PORT` | `8080` | Cloud Run probes this |
| `INFERENCE_CLOUD_LOGGING` | `false` | Enable Cloud Logging integration |

## Local development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=src
export INFERENCE_REDIS_HOST=127.0.0.1
export INFERENCE_PUBSUB_DETECTIONS_TOPIC=projects/your-proj/topics/drone-sensor-dev-detections
python -m inference.main
```

## Container build

```
docker build -f backend/inference/Dockerfile -t drone-inference:dev .
```

## Notes

- Uses TF Hub yamnet/1 (~17 MB). First run downloads it; in the container it's cached under `/app/tfhub_cache`.
- The "Drone" AudioSet class is the primary signal in this session. Session 4 trains a small classifier on YAMNet embeddings for better precision against helicopters/aircraft/lawn equipment.
