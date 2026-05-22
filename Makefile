# Build + deploy the backend services using Cloud Build.
# Nothing builds on the developer machine — the only requirement is
# `gcloud auth login` against the target project.

PROJECT_ID ?= drone-audio-sensor
REGION     ?= us-central1
TAG        ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

GCLOUD_FLAGS := \
	--project=$(PROJECT_ID) \
	--region=$(REGION) \
	--substitutions=_TAG=$(TAG)

ADMIN_IMAGE := us-central1-docker.pkg.dev/$(PROJECT_ID)/drone-sensor-images-dev/admin:$(TAG)
TF_DIR      := iac/terraform

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  make build-admin           Build + push admin image to Artifact Registry"
	@echo "  make deploy-admin          build-admin + terraform apply (admin only)"
	@echo "  make deploy-gateway        Build + push + Cloud Run deploy backend/gateway"
	@echo "  make deploy-inference      Build + push + Cloud Run deploy backend/inference"
	@echo "  make deploy-tak-publisher  Build + push + Cloud Run deploy backend/tak_publisher"
	@echo "  make deploy-all            All four in sequence"
	@echo "  make show                  Show live URLs + image tags for all four services"
	@echo ""
	@echo "Variables (override on the make command line):"
	@echo "  PROJECT_ID  default $(PROJECT_ID)"
	@echo "  REGION      default $(REGION)"
	@echo "  TAG         default short git sha = $(TAG)"
	@echo ""
	@echo "Cloud Build does all the work in GCP — no local Docker required."
	@echo ""
	@echo "Admin uses Terraform-managed deploy (no ignore_changes), so the"
	@echo "deploy step calls 'terraform apply -var admin_image=...' after"
	@echo "the Cloud Build push. The other services use the per-yaml deploy"
	@echo "step because their Cloud Run resources have ignore_changes on image."

.PHONY: build-admin
build-admin:
	gcloud builds submit . $(GCLOUD_FLAGS) --config=cloudbuild/admin.yaml

.PHONY: deploy-admin
deploy-admin: build-admin
	cd $(TF_DIR) && terraform apply -auto-approve -var admin_image=$(ADMIN_IMAGE)

.PHONY: deploy-gateway
deploy-gateway:
	gcloud builds submit . $(GCLOUD_FLAGS) --config=cloudbuild/gateway.yaml

.PHONY: deploy-inference
deploy-inference:
	gcloud builds submit . $(GCLOUD_FLAGS) --config=cloudbuild/inference.yaml

.PHONY: deploy-tak-publisher
deploy-tak-publisher:
	gcloud builds submit . $(GCLOUD_FLAGS) --config=cloudbuild/tak_publisher.yaml

.PHONY: deploy-all
deploy-all: deploy-admin deploy-gateway deploy-tak-publisher deploy-inference

# Show the deployed URLs and image tags so you can sanity-check what's live.
.PHONY: show
show:
	@for svc in drone-sensor-dev-admin drone-sensor-dev-gateway \
	            drone-sensor-dev-inference drone-sensor-dev-tak-publisher; do \
	    echo "===== $$svc ====="; \
	    gcloud run services describe $$svc \
	        --project=$(PROJECT_ID) --region=$(REGION) \
	        --format='value(status.url, spec.template.spec.containers[0].image, status.latestReadyRevisionName)'; \
	done
