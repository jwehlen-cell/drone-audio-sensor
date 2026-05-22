resource "google_service_account" "gateway" {
  account_id   = "${var.resource_prefix}-gw-${var.environment}"
  display_name = "Drone sensor gateway (Cloud Run)"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "inference" {
  account_id   = "${var.resource_prefix}-inf-${var.environment}"
  display_name = "Drone sensor inference worker (placeholder for Session 3)"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "tak_publisher" {
  account_id   = "${var.resource_prefix}-tak-${var.environment}"
  display_name = "Drone sensor TAK publisher (placeholder for Session 4)"

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "gateway_firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "gateway_logging_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "gateway_metrics_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "gateway_trace_writer" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_pubsub_topic_iam_member" "gateway_pubsub_publisher" {
  topic  = google_pubsub_topic.detections.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_device_secret_accessor" {
  secret_id = google_secret_manager_secret.device_bootstrap.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_artifact_registry_repository_iam_member" "gateway_image_reader" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.gateway.email}"
}
