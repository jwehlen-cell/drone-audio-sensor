# Backend Services

- [`gateway/`](gateway/) — Python asyncio gRPC server that accepts phone streams (Session 2). Also publishes each AudioFrame to the Redis Stream consumed by inference.
- [`inference/`](inference/) — YAMNet inference worker pool: Redis Streams consumer, per-device score smoothing, suppression window, Pub/Sub detection events (Session 3)
- [`tak_publisher/`](tak_publisher/) — Subscribes to the detections Pub/Sub topic, converts each event to CoT XML, streams to a TAK Server over persistent TCP/TLS (Session 4)
