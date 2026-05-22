locals {
  name_suffix    = "${var.resource_prefix}-${var.environment}"
  redis_full_url = "redis://${google_redis_instance.cache.host}:${google_redis_instance.cache.port}"

  common_labels = merge(
    var.labels,
    {
      environment = var.environment
    },
  )
}
