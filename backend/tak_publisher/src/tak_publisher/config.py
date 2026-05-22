from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TAK_PUBLISHER_",
        case_sensitive=False,
        protected_namespaces=(),
    )

    gcp_project_id: str = ""
    detections_subscription: str = ""

    tak_credentials_secret: str = ""
    tak_default_host: str = ""
    tak_default_port: int = 8089
    tak_use_tls: bool = True
    tak_connect_timeout_seconds: float = 15.0
    tak_write_timeout_seconds: float = 10.0
    tak_max_reconnect_delay_seconds: float = 60.0

    cot_event_type: str = "a-u-A"
    cot_how: str = "m-g"
    cot_stale_seconds: int = 180
    cot_uid_prefix: str = "drone-detection"
    cot_group_name: str = "Cyan"

    dedup_window_seconds: int = 120
    dedup_cache_size: int = 1024

    health_host: str = "0.0.0.0"
    health_port: int = 8080

    log_level: str = "INFO"
    structured_logs: bool = True
    cloud_logging: bool = False


settings = Settings()
