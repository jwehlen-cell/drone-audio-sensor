locals {
  gateway_image_resolved = var.gateway_image != "" ? var.gateway_image : "us-docker.pkg.dev/cloudrun/container/hello"
}

resource "google_cloud_run_v2_service" "gateway" {
  name     = "${local.name_suffix}-gateway"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.common_labels

  template {
    labels                           = local.common_labels
    service_account                  = google_service_account.gateway.email
    timeout                          = "${var.gateway_request_timeout_seconds}s"
    max_instance_request_concurrency = 1000
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      min_instance_count = var.gateway_min_instances
      max_instance_count = var.gateway_max_instances
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.gateway_image_resolved

      ports {
        name           = "h2c"
        container_port = 50051
      }

      resources {
        limits = {
          cpu    = var.gateway_cpu
          memory = var.gateway_memory
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "GATEWAY_GRPC_HOST"
        value = "0.0.0.0"
      }
      env {
        name  = "GATEWAY_GRPC_PORT"
        value = "50051"
      }
      env {
        name  = "GATEWAY_GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GATEWAY_REDIS_HOST"
        value = google_redis_instance.cache.host
      }
      env {
        name  = "GATEWAY_REDIS_PORT"
        value = tostring(google_redis_instance.cache.port)
      }
      env {
        name  = "GATEWAY_PUBSUB_DETECTIONS_TOPIC"
        value = google_pubsub_topic.detections.id
      }
      env {
        name  = "GATEWAY_DEVICES_COLLECTION"
        value = "devices"
      }
      env {
        name  = "GATEWAY_CLOUD_LOGGING"
        value = "true"
      }
      env {
        name  = "GATEWAY_STRUCTURED_LOGS"
        value = "true"
      }
      env {
        name  = "GATEWAY_REQUIRE_AUTH"
        value = tostring(var.gateway_require_auth)
      }
      env {
        name  = "GATEWAY_JWT_AUDIENCE"
        value = var.gateway_jwt_audience
      }

      startup_probe {
        tcp_socket {
          port = 50051
        }
        initial_delay_seconds = 5
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 6
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
    google_firestore_database.default,
    google_vpc_access_connector.connector,
  ]

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated_invocations ? 1 : 0
  project  = var.project_id
  location = google_cloud_run_v2_service.gateway.location
  name     = google_cloud_run_v2_service.gateway.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
