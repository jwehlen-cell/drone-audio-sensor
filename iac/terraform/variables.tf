variable "project_id" {
  description = "GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "Primary GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Primary zone within the region."
  type        = string
  default     = "us-central1-a"
}

variable "environment" {
  description = "Environment label (dev, test, prod). Used as a name suffix."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "environment must be one of dev, test, prod."
  }
}

variable "resource_prefix" {
  description = "Prefix applied to all named resources."
  type        = string
  default     = "drone-sensor"
}

variable "gateway_image" {
  description = "Container image for the gateway (e.g. <region>-docker.pkg.dev/<project>/<repo>/gateway:<tag>)."
  type        = string
  default     = ""
}

variable "gateway_min_instances" {
  description = "Cloud Run min instance count for the gateway."
  type        = number
  default     = 1
}

variable "gateway_max_instances" {
  description = "Cloud Run max instance count for the gateway."
  type        = number
  default     = 5
}

variable "gateway_cpu" {
  description = "vCPU per gateway instance."
  type        = string
  default     = "2"
}

variable "gateway_memory" {
  description = "Memory per gateway instance."
  type        = string
  default     = "2Gi"
}

variable "gateway_request_timeout_seconds" {
  description = "Cloud Run request timeout in seconds (max 3600 for Gen2)."
  type        = number
  default     = 3600
}

variable "inference_image" {
  description = "Container image for the inference worker (e.g. <region>-docker.pkg.dev/<project>/<repo>/inference:<tag>)."
  type        = string
  default     = ""
}

variable "inference_min_instances" {
  description = "Cloud Run min instance count for inference workers. Keep > 0 so the consumer loop runs continuously."
  type        = number
  default     = 1
}

variable "inference_max_instances" {
  description = "Cloud Run max instance count for inference workers."
  type        = number
  default     = 4
}

variable "inference_cpu" {
  description = "vCPU per inference instance. YAMNet inference is CPU-bound; 4 vCPU handles ~50 frames/sec."
  type        = string
  default     = "4"
}

variable "inference_memory" {
  description = "Memory per inference instance (TF needs headroom)."
  type        = string
  default     = "4Gi"
}

variable "inference_detection_threshold" {
  description = "Drone-class probability threshold for detection trigger."
  type        = number
  default     = 0.5
}

variable "inference_suppression_window_seconds" {
  description = "Seconds to suppress further detections from a device after one fires."
  type        = number
  default     = 60
}

variable "tak_publisher_image" {
  description = "Container image for the TAK publisher (e.g. <region>-docker.pkg.dev/<project>/<repo>/tak-publisher:<tag>)."
  type        = string
  default     = ""
}

variable "tak_publisher_min_instances" {
  description = "Cloud Run min instance count for the TAK publisher (keep > 0 so the Pub/Sub subscriber + TLS socket stay alive)."
  type        = number
  default     = 1
}

variable "tak_publisher_max_instances" {
  description = "Cloud Run max instance count for the TAK publisher."
  type        = number
  default     = 2
}

variable "cot_event_type" {
  description = "CoT event type written to TAK."
  type        = string
  default     = "a-u-A"
}

variable "cot_stale_seconds" {
  description = "How long a CoT marker should remain on TAK clients before going stale."
  type        = number
  default     = 180
}

variable "subscription_max_delivery_attempts" {
  description = "Number of times Pub/Sub will retry delivery before sending to DLQ."
  type        = number
  default     = 5
}

variable "admin_image" {
  description = "Container image for the admin UI."
  type        = string
  default     = ""
}

variable "admin_max_instances" {
  description = "Cloud Run max instance count for the admin UI. Scales to zero when idle."
  type        = number
  default     = 2
}

variable "admin_invoker_members" {
  description = "Principals (user:..., group:..., serviceAccount:...) granted run.invoker on the admin Cloud Run service. Leave empty to require manual binding via gcloud or the console. Ignored when admin_allow_unauthenticated_invocations = true."
  type        = list(string)
  default     = []
}

variable "admin_allow_unauthenticated_invocations" {
  description = "When true (R&D default), the admin Cloud Run service accepts unauthenticated calls and the server-side IAM check is disabled. Set to false in production and populate admin_invoker_members."
  type        = bool
  default     = true
}

variable "redis_tier" {
  description = "Memorystore tier (BASIC for R&D, STANDARD_HA for production)."
  type        = string
  default     = "BASIC"
}

variable "redis_memory_gb" {
  description = "Memorystore Redis memory size in GB."
  type        = number
  default     = 1
}

variable "vpc_connector_cidr" {
  description = "CIDR for the Serverless VPC Access connector subnet (must be /28)."
  type        = string
  default     = "10.8.0.0/28"
}

variable "allow_unauthenticated_invocations" {
  description = "Whether the Cloud Run service allows unauthenticated invocations. The gateway runs its own JWT auth (Keystore-backed device identity), so the Cloud Run IAM layer stays open."
  type        = bool
  default     = true
}

variable "gateway_require_auth" {
  description = "Whether the gateway enforces device JWT validation. Set to true once devices have been provisioned with public keys in Firestore."
  type        = bool
  default     = false
}

variable "gateway_jwt_audience" {
  description = "Expected JWT audience claim (typically the gateway's https URL). Required when gateway_require_auth = true."
  type        = string
  default     = ""
}

variable "labels" {
  description = "Common labels applied to all labeled resources."
  type        = map(string)
  default = {
    system  = "drone-sensor"
    managed = "terraform"
  }
}
