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
  description = "Whether the Cloud Run service allows unauthenticated invocations. Should be true for R&D so phones can connect without OIDC tokens."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Common labels applied to all labeled resources."
  type        = map(string)
  default = {
    system  = "drone-sensor"
    managed = "terraform"
  }
}
