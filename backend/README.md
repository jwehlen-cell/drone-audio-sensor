# Backend Services

- [`gateway/`](gateway/) — Python asyncio gRPC server that accepts phone streams (Session 2). Now also publishes each AudioFrame to the Redis Stream consumed by inference.
- [`inference/`](inference/) — YAMNet inference worker pool: Redis Streams consumer, per-device score smoothing, suppression window, Pub/Sub detection events (Session 3)

Future:

- `tak_publisher/` — CoT XML publisher to TAK Server (Session 4)
