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
    firestore_database: str = "(default)"

    model_handle: str = "https://tfhub.dev/google/yamnet/1"
    model_name: str = "yamnet"
    model_version: str = "1"
    score_buffer_size: int = 5
    detection_threshold: float = 0.5
    min_frames_over_threshold: int = 3
    suppression_window_seconds: int = 60

    drone_class_names: tuple[str, ...] = (
        "Drone",
    )
    auxiliary_class_names: tuple[str, ...] = (
        "Helicopter",
        "Aircraft",
        "Fixed-wing aircraft, airplane",
        "Aircraft engine",
        "Propeller, airscrew",
    )

    health_host: str = "0.0.0.0"
    health_port: int = 8080

    log_level: str = "INFO"
    structured_logs: bool = True
    cloud_logging: bool = False


settings = Settings()
