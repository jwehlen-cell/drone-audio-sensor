resource "google_secret_manager_secret" "device_bootstrap" {
  secret_id = "${local.name_suffix}-device-bootstrap"

  replication {
    auto {}
  }

  labels = local.common_labels

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret" "tak_server_credentials" {
  secret_id = "${local.name_suffix}-tak-credentials"

  replication {
    auto {}
  }

  labels = local.common_labels

  depends_on = [google_project_service.enabled]
}
