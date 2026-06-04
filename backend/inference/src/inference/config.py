from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INFERENCE_",
        case_sensitive=False,
        protected_namespaces=(),
    )

    gcp_project_id: str = ""

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_ttl_seconds: int = 600

    frame_stream_key: str = "audio_frames"
    consumer_group: str = "inference"
    consumer_name: str = "worker"
    read_batch_size: int = 8
    read_block_ms: int = 1000

    pubsub_detections_topic: str = ""
    devices_collection: str = "devices"
    detections_collection: str = "detections"
    detection_doc_ttl_seconds: int = 86400  # 24 h
    firestore_database: str = "(default)"

    model_handle: str = "https://tfhub.dev/google/yamnet/1"
    # Trained dense heads over YAMNet embeddings, sourced from
    # github.com/jwehlen-cell/yamnet-drone-detector and baked into the
    # container at /app/models/. Binary drives the detection threshold;
    # subtype runs alongside and characterizes which drone model is
    # present when a detection fires.
    dense_classifier_path: str = "/app/models/drone_classifier_binary.keras"
    subtype_classifier_path: str = "/app/models/drone_classifier_subtype.keras"
    subtype_labels_path: str = "/app/models/drone_classifier_subtype.labels.json"
    # "Angels Envy" is the QST + USAFA + night-negatives retrain, now also
    # trained on 5,000 Kilo-verified false-positive HARD NEGATIVES harvested
    # from 8 h of live Shaw audio (yamnet-drone-detector commit 9729a32). Names
    # persist across retrains as long as the dataset family + architecture are
    # the same — bump the name only when a new training family takes over.
    model_name: str = "Angels Envy"
    model_version: str = "angels-envy-2026.06-hardneg"
    score_buffer_size: int = 5
    # Retuned 0.5 -> 0.70 for the hard-negative model (retune_threshold.py):
    # the model scores confirmed drones ~1.0 (USAFA min 0.997) so detection
    # survives 100% at 0.70, while held-out field confounders crater
    # (median 0.014, p95 0.46) -> false-alarm rate on the deployment domain
    # drops ~33x vs the old model at 0.5 (47% -> 1.4%).
    detection_threshold: float = 0.70
    # Cadence-aware detection gate. Instead of "K of N frames above
    # threshold" (which silently misses anything from a station whose
    # cadence is wider than the typical flyby), we accumulate
    # seconds-of-audio above threshold across the buffer. A 1 s station
    # needs 3 frames > 0.5 to fire (3 × 1 s = 3 s); a 5 s station fires
    # on a single positive frame (5 s ≥ 3 s); a 30 s station likewise
    # fires on one (30 s ≥ 3 s). Per-device suppression + the buffer
    # clear on trigger prevent re-fires from stale frames.
    min_seconds_over_threshold: float = 3.0
    suppression_window_seconds: int = 60

    # Kept as a diagnostic side channel only. The trained dense head produces
    # the actual drone_score; these YAMNet AudioSet labels just give downstream
    # consumers extra context about what the audio sounded like.
    auxiliary_class_names: tuple[str, ...] = (
        "Helicopter",
        "Aircraft",
        "Fixed-wing aircraft, airplane",
        "Aircraft engine",
        "Propeller, airscrew",
    )

    health_host: str = "0.0.0.0"
    health_port: int = 8080

    # Watchdog: if the worker hasn't processed a frame in this many
    # seconds, log a critical event and exit the process so Cloud Run
    # replaces the container. Catches the silent-hang failure mode
    # observed 2026-05-31 where the Redis stream consumer blocked
    # while the HTTP /healthz probe kept returning 200 (separate
    # thread), so Cloud Run never knew to replace the instance.
    #
    # Default 300 s is well above the simulator's 3-min cycle but well
    # below an acceptable downtime window. Set to 0 to disable (e.g.
    # for environments with long idle periods between bursts).
    watchdog_stall_seconds: int = 300
    # How often the watchdog wakes up to check liveness. Auto-derived
    # from watchdog_stall_seconds if left at the default 0.
    watchdog_check_interval_seconds: int = 0

    log_level: str = "INFO"
    structured_logs: bool = True
    cloud_logging: bool = False


settings = Settings()
