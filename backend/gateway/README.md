# Gateway

Python asyncio gRPC server that accepts persistent audio streams from the phone app, validates the handshake, persists device registration to Firestore, and maintains hot per-device state in Redis.

## Local development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash scripts/generate_protos.sh        # populates proto_gen/
export PYTHONPATH="src:proto_gen"
export GATEWAY_GCP_PROJECT_ID=your-project
export GATEWAY_REDIS_HOST=127.0.0.1
python -m gateway.main
```

For a fully local loop without GCP, run a Firestore emulator + Redis:

```
gcloud emulators firestore start --host-port=127.0.0.1:8080 &
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
docker run --rm -p 6379:6379 redis:7-alpine &
```

## Container build

The Dockerfile is configured to be built from the **repo root** so it can pull in `proto/`:

```
docker build -f backend/gateway/Dockerfile -t drone-gateway:dev .
```

## Configuration

Environment variables (all `GATEWAY_`-prefixed):

| Variable | Default | Notes |
|---|---|---|
| `GATEWAY_GRPC_HOST` | `0.0.0.0` | Bind host |
| `GATEWAY_GRPC_PORT` | `50051` | Bind port (Cloud Run injects `PORT`) |
| `GATEWAY_GCP_PROJECT_ID` | empty | Required when not on GCE/Cloud Run |
| `GATEWAY_FIRESTORE_DATABASE` | `(default)` | Firestore database id |
| `GATEWAY_DEVICES_COLLECTION` | `devices` | Firestore collection name |
| `GATEWAY_REDIS_HOST` | `127.0.0.1` | Memorystore host or local Redis |
| `GATEWAY_REDIS_PORT` | `6379` | Redis port |
| `GATEWAY_REDIS_TTL_SECONDS` | `300` | TTL on per-device state |
| `GATEWAY_ACK_INTERVAL_FRAMES` | `10` | Send FrameAck every N frames |
| `GATEWAY_CLOUD_LOGGING` | `false` | Enable Cloud Logging integration |
