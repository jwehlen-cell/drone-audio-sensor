resource "google_logging_project_bucket_config" "default" {
  project        = var.project_id
  location       = "global"
  retention_days = 30
  bucket_id      = "_Default"
}

resource "google_monitoring_alert_policy" "gateway_5xx" {
  display_name = "${local.name_suffix} gateway 5xx burst"
  combiner     = "OR"

  conditions {
    display_name = "5xx > 5/min"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.label.\"response_code_class\" = \"5xx\" AND resource.label.\"service_name\" = \"${google_cloud_run_v2_service.gateway.name}\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = []

  enabled = true

  depends_on = [google_project_service.enabled]
}
