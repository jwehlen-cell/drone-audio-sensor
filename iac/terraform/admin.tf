locals {
  admin_image_resolved = var.admin_image != "" ? var.admin_image : "us-docker.pkg.dev/cloudrun/container/hello"
}

resource "google_service_account" "admin" {
  account_id   = "${var.resource_prefix}-admin-${var.environment}"
  display_name = "Drone sensor admin UI"

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "admin_firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_logging_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_metrics_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_artifact_registry_repository_iam_member" "admin_image_reader" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_cloud_run_v2_service" "admin" {
  name     = "${local.name_suffix}-admin"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.common_labels

  template {
    labels                           = local.common_labels
    service_account                  = google_service_account.admin.email
    max_instance_request_concurrency = 80
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    # Scale to zero — admin UI is human-driven, not a hot path.
    scaling {
      min_instance_count = 0
      max_instance_count = var.admin_max_instances
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.admin_image_resolved

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ADMIN_GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "ADMIN_REDIS_HOST"
        value = google_redis_instance.cache.host
      }
      env {
        name  = "ADMIN_REDIS_PORT"
        value = tostring(google_redis_instance.cache.port)
      }
      env {
        name  = "ADMIN_DEVICES_COLLECTION"
        value = "devices"
      }
      env {
        name  = "ADMIN_DETECTIONS_COLLECTION"
        value = "detections"
      }
      env {
        name  = "ADMIN_CLOUD_LOGGING"
        value = "true"
      }
      env {
        name  = "ADMIN_STRUCTURED_LOGS"
        value = "true"
      }
      # IAM enforcement toggle. R&D environments leave this `false` so
      # the SOH page can be opened from a laptop without an identity
      # token; production should set this `true` and pair it with
      # admin_allow_unauthenticated_invocations = false in Terraform.
      env {
        name  = "ADMIN_REQUIRE_AUTH"
        value = tostring(!var.admin_allow_unauthenticated_invocations)
      }
      env {
        name  = "ADMIN_SIMULATOR_ENABLED"
        value = tostring(var.admin_simulator_enabled)
      }
      env {
        name  = "ADMIN_SIMULATOR_TOKEN"
        value = var.admin_simulator_token
      }
      env {
        name  = "ADMIN_STATUS_REFRESH_SECONDS"
        value = tostring(var.admin_status_refresh_seconds)
      }
    }
  }

  traffic {
    percent = 100
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
  }

  depends_on = [
    google_project_service.enabled,
    google_firestore_database.default,
    google_vpc_access_connector.connector,
  ]

  # NOTE: no `ignore_changes` on the image — for the admin web UI we
  # want Terraform to roll new revisions during R&D. Set var.admin_image
  # to the tag you just pushed and run `terraform apply` to deploy.
}

# Production path: grant invoker only to the principals listed in
# admin_invoker_members. Stays in place even in R&D so flipping the
# unauthenticated flag back off doesn't require re-listing operators.
resource "google_cloud_run_v2_service_iam_member" "admin_invokers" {
  for_each = toset(var.admin_invoker_members)
  project  = var.project_id
  location = google_cloud_run_v2_service.admin.location
  name     = google_cloud_run_v2_service.admin.name
  role     = "roles/run.invoker"
  member   = each.value
}

# R&D path: when admin_allow_unauthenticated_invocations is true, grant
# `allUsers` the invoker role so the SOH page is reachable without an
# identity token. To re-enable IAM, flip the variable to false and run
# `terraform apply`; this resource disappears and the prod-style
# `admin_invokers` bindings take over.
resource "google_cloud_run_v2_service_iam_member" "admin_public_invoker" {
  count    = var.admin_allow_unauthenticated_invocations ? 1 : 0
  project  = var.project_id
  location = google_cloud_run_v2_service.admin.location
  name     = google_cloud_run_v2_service.admin.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Firestore TTL on the detections collection — keeps the admin "recent
# detections" query cheap by auto-deleting rows whose `expires_at` field
# is in the past.
resource "google_firestore_field" "detections_ttl" {
  project    = var.project_id
  database   = google_firestore_database.default.name
  collection = "detections"
  field      = "expires_at"

  ttl_config {}

  index_config {
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION"
    }
    indexes {
      order       = "DESCENDING"
      query_scope = "COLLECTION"
    }
  }

  depends_on = [google_firestore_database.default]
}
