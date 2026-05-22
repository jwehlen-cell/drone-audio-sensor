locals {
  inference_image_resolved = var.inference_image != "" ? var.inference_image : "us-docker.pkg.dev/cloudrun/container/hello"
}

resource "google_cloud_run_v2_service" "inference" {
  name     = "${local.name_suffix}-inference"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = local.common_labels

  template {
    labels                           = local.common_labels
    service_account                  = google_service_account.inference.email
    timeout                          = "3600s"
    max_instance_request_concurrency = 1
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      min_instance_count = var.inference_min_instances
      max_instance_count = var.inference_max_instances
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.inference_image_resolved

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.inference_cpu
          memory = var.inference_memory
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "INFERENCE_GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "INFERENCE_REDIS_HOST"
        value = google_redis_instance.cache.host
      }
      env {
        name  = "INFERENCE_REDIS_PORT"
        value = tostring(google_redis_instance.cache.port)
      }
      env {
        name  = "INFERENCE_FRAME_STREAM_KEY"
        value = "audio_frames"
      }
      env {
        name  = "INFERENCE_CONSUMER_GROUP"
        value = "inference"
      }
      env {
        name  = "INFERENCE_PUBSUB_DETECTIONS_TOPIC"
        value = google_pubsub_topic.detections.id
      }
      env {
        name  = "INFERENCE_DETECTION_THRESHOLD"
        value = tostring(var.inference_detection_threshold)
      }
      env {
        name  = "INFERENCE_SUPPRESSION_WINDOW_SECONDS"
        value = tostring(var.inference_suppression_window_seconds)
      }
      env {
        name  = "INFERENCE_HEALTH_PORT"
        value = "8080"
      }
      env {
        name  = "INFERENCE_CLOUD_LOGGING"
        value = "true"
      }
      env {
        name  = "INFERENCE_STRUCTURED_LOGS"
        value = "true"
      }

      startup_probe {
        http_get {
          path = "/readyz"
          port = 8080
        }
        initial_delay_seconds = 30
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 12
      }

      liveness_probe {
        http_get {
          path = "/livez"
          port = 8080
        }
        timeout_seconds   = 5
        period_seconds    = 30
        failure_threshold = 3
      }
    }
  }

  traffic {
    percent = 100
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
  }

  depends_on = [
    google_project_service.enabled,
    google_redis_instance.cache,
    google_pubsub_topic.detections,
    google_vpc_access_connector.connector,
  ]

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}
