resource "google_logging_project_bucket_config" "default" {
  project        = var.project_id
  location       = "global"
  retention_days = 30
  bucket_id      = "_Default"
}

# ---------------------------------------------------------------------------
# Alert policies
# ---------------------------------------------------------------------------

locals {
  cr_filter = "resource.type = \"cloud_run_revision\""
}

resource "google_monitoring_alert_policy" "gateway_5xx" {
  display_name = "${local.name_suffix}: gateway 5xx burst"
  combiner     = "OR"

  conditions {
    display_name = "Gateway 5xx > 5/min"
    condition_threshold {
      filter          = "${local.cr_filter} AND metric.type = \"run.googleapis.com/request_count\" AND metric.label.\"response_code_class\" = \"5xx\" AND resource.label.\"service_name\" = \"${google_cloud_run_v2_service.gateway.name}\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  documentation {
    content   = "Gateway is returning 5xx at >5/min. Check Cloud Run logs for the gateway service; common causes are Redis connectivity (VPC connector) or Firestore quota."
    mime_type = "text/markdown"
  }

  enabled = true
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "gateway_latency_p95" {
  display_name = "${local.name_suffix}: gateway request latency p95"
  combiner     = "OR"

  conditions {
    display_name = "Gateway p95 latency > 5s"
    condition_threshold {
      filter          = "${local.cr_filter} AND metric.type = \"run.googleapis.com/request_latencies\" AND resource.label.\"service_name\" = \"${google_cloud_run_v2_service.gateway.name}\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5000
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MEAN"
      }
    }
  }

  documentation {
    content   = "Gateway request handler p95 > 5s sustained. With persistent gRPC streams this metric is dominated by long-lived calls; investigate only if it's a fresh spike."
    mime_type = "text/markdown"
  }

  enabled    = true
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "instance_saturation" {
  display_name = "${local.name_suffix}: Cloud Run at max instances"
  combiner     = "OR"

  conditions {
    display_name = "Active instances at configured max for 10m"
    condition_threshold {
      filter          = "${local.cr_filter} AND metric.type = \"run.googleapis.com/container/instance_count\""
      duration        = "600s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.gateway_max_instances - 1
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MEAN"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    content   = "A Cloud Run service has been pinned at its max instance count for 10 minutes. Either traffic genuinely grew or there's a slow request leak."
    mime_type = "text/markdown"
  }

  enabled    = true
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "redis_memory" {
  display_name = "${local.name_suffix}: Memorystore memory > 75%"
  combiner     = "OR"

  conditions {
    display_name = "Used memory ratio > 75%"
    condition_threshold {
      filter          = "resource.type = \"redis_instance\" AND metric.type = \"redis.googleapis.com/stats/memory/usage_ratio\" AND resource.label.\"instance_id\" = \"${google_redis_instance.cache.name}\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.75
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    content   = "Memorystore is filling up. Frame stream maxlen or per-device TTL may need tightening, or the instance tier should be increased."
    mime_type = "text/markdown"
  }

  enabled    = true
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "pubsub_backlog" {
  display_name = "${local.name_suffix}: detections subscription backlog"
  combiner     = "OR"

  conditions {
    display_name = "Oldest unacked > 5 min"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" AND resource.label.\"subscription_id\" = \"${google_pubsub_subscription.tak_publisher.name}\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 300
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    content   = "TAK publisher is not draining the detections subscription. Check the publisher logs — most likely the TAK Server is unreachable or rejecting the certificate."
    mime_type = "text/markdown"
  }

  enabled    = true
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "dlq_present" {
  display_name = "${local.name_suffix}: detections DLQ receiving messages"
  combiner     = "OR"

  conditions {
    display_name = "Any messages on the DLQ topic"
    condition_threshold {
      filter          = "resource.type = \"pubsub_topic\" AND metric.type = \"pubsub.googleapis.com/topic/send_message_operation_count\" AND resource.label.\"topic_id\" = \"${google_pubsub_topic.detections_dlq.name}\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  documentation {
    content   = "Messages are landing in the dead-letter queue. Pull them with `gcloud pubsub subscriptions pull` against the DLQ topic to inspect failures."
    mime_type = "text/markdown"
  }

  enabled    = true
  depends_on = [google_project_service.enabled]
}

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

resource "google_monitoring_dashboard" "drone_sensor" {
  dashboard_json = jsonencode({
    displayName = "${local.name_suffix} — drone sensor"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width  = 6
          height = 4
          widget = {
            title = "Gateway request rate (by response class)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter             = "resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${google_cloud_run_v2_service.gateway.name}\" AND metric.type=\"run.googleapis.com/request_count\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.\"response_code_class\""]
                    }
                  }
                }
                plotType = "STACKED_BAR"
              }]
            }
          }
        },
        {
          xPos   = 6
          width  = 6
          height = 4
          widget = {
            title = "Gateway request latency (p50 / p95 / p99)"
            xyChart = {
              dataSets = [
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${google_cloud_run_v2_service.gateway.name}\" AND metric.type=\"run.googleapis.com/request_latencies\""
                      aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_50" }
                    }
                  }
                  plotType = "LINE"
                },
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${google_cloud_run_v2_service.gateway.name}\" AND metric.type=\"run.googleapis.com/request_latencies\""
                      aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_95" }
                    }
                  }
                  plotType = "LINE"
                },
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${google_cloud_run_v2_service.gateway.name}\" AND metric.type=\"run.googleapis.com/request_latencies\""
                      aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_99" }
                    }
                  }
                  plotType = "LINE"
                },
              ]
            }
          }
        },
        {
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "Cloud Run instance counts"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/container/instance_count\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_MAX"
                      groupByFields      = ["resource.label.\"service_name\""]
                    }
                  }
                }
                plotType = "LINE"
              }]
            }
          }
        },
        {
          xPos   = 6
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "Memorystore memory usage"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"redis_instance\" AND metric.type=\"redis.googleapis.com/stats/memory/usage_ratio\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_MEAN" }
                  }
                }
                plotType = "LINE"
              }]
            }
          }
        },
        {
          yPos   = 8
          width  = 6
          height = 4
          widget = {
            title = "Detections topic: published / acked"
            xyChart = {
              dataSets = [
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"pubsub_topic\" AND resource.label.\"topic_id\"=\"${google_pubsub_topic.detections.name}\" AND metric.type=\"pubsub.googleapis.com/topic/send_message_operation_count\""
                      aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE" }
                    }
                  }
                  plotType = "LINE"
                },
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"pubsub_subscription\" AND resource.label.\"subscription_id\"=\"${google_pubsub_subscription.tak_publisher.name}\" AND metric.type=\"pubsub.googleapis.com/subscription/ack_message_count\""
                      aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE" }
                    }
                  }
                  plotType = "LINE"
                },
              ]
            }
          }
        },
        {
          xPos   = 6
          yPos   = 8
          width  = 6
          height = 4
          widget = {
            title = "TAK subscription backlog (oldest unacked, seconds)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"pubsub_subscription\" AND resource.label.\"subscription_id\"=\"${google_pubsub_subscription.tak_publisher.name}\" AND metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_MEAN" }
                  }
                }
                plotType = "LINE"
              }]
            }
          }
        },
      ]
    }
  })
}
