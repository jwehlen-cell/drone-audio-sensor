output "gateway_url" {
  description = "HTTPS URL of the gateway Cloud Run service (also serves gRPC over HTTP/2 on :443)."
  value       = google_cloud_run_v2_service.gateway.uri
}

output "gateway_service_account" {
  description = "Gateway runtime service account email."
  value       = google_service_account.gateway.email
}

output "redis_host" {
  description = "Memorystore Redis host (reachable from the VPC connector only)."
  value       = google_redis_instance.cache.host
}

output "redis_port" {
  description = "Memorystore Redis port."
  value       = google_redis_instance.cache.port
}

output "artifact_registry_repo" {
  description = "Docker repository to push gateway images to."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "detections_topic" {
  description = "Pub/Sub topic for confirmed drone detections (consumed in Session 4)."
  value       = google_pubsub_topic.detections.id
}

output "device_secret_name" {
  description = "Secret Manager secret holding device-bootstrap material (placeholder for Session 5)."
  value       = google_secret_manager_secret.device_bootstrap.id
}
