resource "google_compute_network" "vpc" {
  name                    = "${local.name_suffix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.enabled]
}

resource "google_compute_subnetwork" "primary" {
  name                     = "${local.name_suffix}-subnet"
  ip_cidr_range            = "10.10.0.0/20"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true
}

resource "google_vpc_access_connector" "connector" {
  name          = substr("${local.name_suffix}-conn", 0, 25)
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = var.vpc_connector_cidr
  min_instances = 2
  max_instances = 4
  machine_type  = "e2-micro"

  depends_on = [google_project_service.enabled]
}

resource "google_compute_global_address" "private_service_range" {
  name          = "${local.name_suffix}-psa-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range.name]

  depends_on = [google_project_service.enabled]
}
