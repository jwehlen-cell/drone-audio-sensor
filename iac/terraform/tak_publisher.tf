locals {
  tak_publisher_image_resolved = var.tak_publisher_image != "" ? var.tak_publisher_image : "us-docker.pkg.dev/cloudrun/container/hello"
}

resource "google_pubsub_subscription" "tak_publisher" {
  name  = "${local.name_suffix}-tak-publisher"
  topic = google_pubsub_topic.detections.name

  ack_deadline_seconds       = 30
  message_retention_duration = "86400s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.detections_dlq.id
    max_delivery_attempts = var.subscription_max_delivery_attempts
  }

  labels = local.common_labels

  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_subscription_iam_member" "tak_subscriber" {
  subscription = google_pubsub_subscription.tak_publisher.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.tak_publisher.email}"
}

resource "google_pubsub_topic_iam_member" "tak_publisher_dlq" {
  topic  = google_pubsub_topic.detections_dlq.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "tak_publisher_logging_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.tak_publisher.email}"
}

resource "google_project_iam_member" "tak_publisher_metrics_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.tak_publisher.email}"
}

resource "google_secret_manager_secret_iam_member" "tak_publisher_credentials_accessor" {
  secret_id = google_secret_manager_secret.tak_server_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tak_publisher.email}"
}

resource "google_artifact_registry_repository_iam_member" "tak_publisher_image_reader" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.tak_publisher.email}"
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_cloud_run_v2_service" "tak_publisher" {
  name     = "${local.name_suffix}-tak-publisher"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = local.common_labels

  template {
    labels                           = local.common_labels
    service_account                  = google_service_account.tak_publisher.email
    timeout                          = "3600s"
    max_instance_request_concurrency = 1
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      min_instance_count = var.tak_publisher_min_instances
      max_instance_count = var.tak_publisher_max_instances
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "ALL_TRAFFIC"
    }

    containers {
      image = local.tak_publisher_image_resolved

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "TAK_PUBLISHER_GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "TAK_PUBLISHER_DETECTIONS_SUBSCRIPTION"
        value = google_pubsub_subscription.tak_publisher.id
      }
      env {
        name  = "TAK_PUBLISHER_TAK_CREDENTIALS_SECRET"
        value = google_secret_manager_secret.tak_server_credentials.id
      }
      env {
        name  = "TAK_PUBLISHER_COT_EVENT_TYPE"
        value = var.cot_event_type
      }
      env {
        name  = "TAK_PUBLISHER_COT_STALE_SECONDS"
        value = tostring(var.cot_stale_seconds)
      }
      env {
        name  = "TAK_PUBLISHER_HEALTH_PORT"
        value = "8080"
      }
      env {
        name  = "TAK_PUBLISHER_CLOUD_LOGGING"
        value = "true"
      }
      env {
        name  = "TAK_PUBLISHER_STRUCTURED_LOGS"
        value = "true"
      }

      startup_probe {
        http_get {
          path = "/readyz"
          port = 8080
        }
        initial_delay_seconds = 5
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 30
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
    google_pubsub_subscription.tak_publisher,
    google_vpc_access_connector.connector,
  ]

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}
