resource "google_pubsub_topic" "detections" {
  name   = "${local.name_suffix}-detections"
  labels = local.common_labels

  message_retention_duration = "86400s" # 1 day

  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "detections_dlq" {
  name   = "${local.name_suffix}-detections-dlq"
  labels = local.common_labels

  depends_on = [google_project_service.enabled]
}
