resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "${var.resource_prefix}-images-${var.environment}"
  description   = "Drone sensor container images"
  format        = "DOCKER"

  labels = local.common_labels

  depends_on = [google_project_service.enabled]
}
