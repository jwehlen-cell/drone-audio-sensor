from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", case_sensitive=False)

    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    grpc_max_concurrent_streams: int = 1000

    gcp_project_id: str = ""
    firestore_database: str = "(default)"
    devices_collection: str = "devices"

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_ttl_seconds: int = 300

    pubsub_detections_topic: str = ""

    frame_stream_key: str = "audio_frames"
    frame_stream_maxlen: int = 3000

    ack_interval_frames: int = 10
    health_report_interval_seconds: int = 30
    session_idle_timeout_seconds: int = 90

    require_auth: bool = False
    jwt_audience: str = ""
    jwt_issuer: str = "drone-sensor"
    jwt_public_key_cache_seconds: int = 300

    log_level: str = "INFO"
    structured_logs: bool = True
    cloud_logging: bool = False


settings = Settings()
