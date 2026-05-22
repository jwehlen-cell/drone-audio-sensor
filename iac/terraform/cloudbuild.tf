# IAM bindings the Cloud Build default service account needs to build
# container images and roll Cloud Run services. Without these the
# `gcloud builds submit ... --config=cloudbuild/*.yaml` invocations
# (driven by the top-level Makefile) fail at the `gcloud run deploy`
# step.
#
# When you move to Pattern B (a Terraform-managed google_cloudbuild_
# trigger), the same SA + bindings apply — the trigger just changes
# WHO invokes the build (a git push vs. a developer running make).

locals {
  cloudbuild_default_sa = "${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

# Push images to Artifact Registry.
resource "google_artifact_registry_repository_iam_member" "cloudbuild_image_writer" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${local.cloudbuild_default_sa}"
}

# Deploy new revisions to Cloud Run services.
resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${local.cloudbuild_default_sa}"
}

# Cloud Build deploys run *as* the runtime SA of each service; this
# binding lets the build SA impersonate them on `gcloud run deploy`.
resource "google_service_account_iam_member" "cloudbuild_act_as_gateway" {
  service_account_id = google_service_account.gateway.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloudbuild_default_sa}"
}

resource "google_service_account_iam_member" "cloudbuild_act_as_inference" {
  service_account_id = google_service_account.inference.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloudbuild_default_sa}"
}

resource "google_service_account_iam_member" "cloudbuild_act_as_tak_publisher" {
  service_account_id = google_service_account.tak_publisher.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloudbuild_default_sa}"
}

resource "google_service_account_iam_member" "cloudbuild_act_as_admin" {
  service_account_id = google_service_account.admin.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloudbuild_default_sa}"
}
