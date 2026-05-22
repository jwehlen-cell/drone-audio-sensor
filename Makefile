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

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  make deploy-admin          Build + push + deploy backend/admin"
	@echo "  make deploy-gateway        Build + push + deploy backend/gateway"
	@echo "  make deploy-inference      Build + push + deploy backend/inference"
	@echo "  make deploy-tak-publisher  Build + push + deploy backend/tak_publisher"
	@echo "  make deploy-all            All four in sequence"
	@echo ""
	@echo "Variables (override on the make command line):"
	@echo "  PROJECT_ID  default $(PROJECT_ID)"
	@echo "  REGION      default $(REGION)"
	@echo "  TAG         default short git sha = $(TAG)"
	@echo ""
	@echo "Cloud Build does all the work in GCP — no local Docker required."

.PHONY: deploy-admin
deploy-admin:
	gcloud builds submit . $(GCLOUD_FLAGS) --config=cloudbuild/admin.yaml

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
