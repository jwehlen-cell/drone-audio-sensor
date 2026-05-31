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
    detection_doc_ttl_seconds: int = 3600
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
    model_name: str = "yamnet+erau-binary+subtype"
    model_version: str = "erau-2024.3-v2"
    score_buffer_size: int = 5
    detection_threshold: float = 0.5
    min_frames_over_threshold: int = 3
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
