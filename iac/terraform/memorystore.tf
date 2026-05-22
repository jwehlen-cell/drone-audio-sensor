resource "google_redis_instance" "cache" {
  name           = "${local.name_suffix}-redis"
  tier           = var.redis_tier
  memory_size_gb = var.redis_memory_gb
  region         = var.region

  authorized_network      = google_compute_network.vpc.id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  reserved_ip_range       = google_compute_global_address.private_service_range.name
  transit_encryption_mode = "DISABLED"
  redis_version           = "REDIS_7_0"
  display_name            = "Drone sensor per-device state cache"

  labels = local.common_labels

  depends_on = [
    google_service_networking_connection.private_vpc,
    google_project_service.enabled,
  ]
}
