from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ADMIN_",
        case_sensitive=False,
        protected_namespaces=(),
    )

    gcp_project_id: str = ""
    firestore_database: str = "(default)"
    devices_collection: str = "devices"
    detections_collection: str = "detections"

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    device_state_key_prefix: str = "device:"

    # `last_seen` thresholds for dashboard color-coding.
    stale_warning_seconds: int = 30
    stale_offline_seconds: int = 300

    # Detection list window — read from Firestore. Bound by the writer's TTL
    # but we additionally filter so the UI doesn't show >1h hits if TTL
    # cleanup hasn't run yet.
    recent_detections_window_seconds: int = 3600
    recent_detections_limit: int = 200

    host: str = "0.0.0.0"
    port: int = 8080

    # IAM/IAP enforcement toggle.
    #
    # Defaults to False so R&D environments can ship without standing up
    # an identity-token flow. Set to True in production (and pair with
    # admin_allow_unauthenticated_invocations=false in Terraform) to
    # require the X-Goog-Authenticated-User-Email header.
    require_auth: bool = False

    # R&D-only simulator endpoint. When enabled, a local laptop script can
    # POST fake phone state to the admin URL; the Cloud Run service writes
    # Firestore + private Redis from inside the VPC connector.
    simulator_enabled: bool = False
    simulator_token: str = ""

    # How often the status page meta-refreshes itself (seconds). Set to 0
    # to disable auto-refresh entirely. Defaults to 30s so dashboards stay
    # current without a noticeable flicker between refreshes.
    status_refresh_seconds: int = 30

    log_level: str = "INFO"
    structured_logs: bool = True
    cloud_logging: bool = False


settings = Settings()
