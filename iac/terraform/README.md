# Terraform — Drone Sensor GCP Infrastructure

Provisions everything the gateway service needs:

- VPC + subnet + Serverless VPC Access connector
- Private Service Access range (for Memorystore peering)
- Memorystore Redis (BASIC tier by default; switch to STANDARD_HA for prod)
- Firestore (Native mode, `(default)` database)
- Pub/Sub topic for confirmed drone detections (consumed in Session 4)
- Secret Manager secrets (device bootstrap material; TAK credentials)
- Artifact Registry Docker repository
- Service accounts for gateway / inference / TAK publisher with least-privilege bindings
- Cloud Run v2 service (gateway) with HTTP/2 ingress, persistent stream timeout, VPC egress

## Prerequisites

- A GCP project (billing enabled)
- `gcloud auth application-default login`
- `terraform >= 1.6`

## First-time apply

```
cd iac/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set project_id at minimum

terraform init
terraform apply
```

The first apply takes ~10 minutes (Memorystore + VPC peering are slow). The Cloud Run service will come up using a placeholder image (`hello`) since you have not pushed `gateway` yet — its `image` field is on `ignore_changes`, so subsequent applies won't fight your `gcloud run deploy`.

## Push the gateway image

From the repo root, after Terraform has created the Artifact Registry repo:

```
PROJECT_ID=$(terraform -chdir=iac/terraform output -raw artifact_registry_repo | cut -d/ -f3)
REPO_URL=$(terraform -chdir=iac/terraform output -raw artifact_registry_repo)
REGION=$(terraform -chdir=iac/terraform output -raw gateway_url | awk -F. '{print $2}')

gcloud auth configure-docker ${REGION}-docker.pkg.dev

docker build -f backend/gateway/Dockerfile -t ${REPO_URL}/gateway:0.1.0 .
docker push ${REPO_URL}/gateway:0.1.0

gcloud run deploy $(terraform -chdir=iac/terraform output -raw gateway_url | sed 's|https://||' | cut -d- -f1-3) \
  --image=${REPO_URL}/gateway:0.1.0 \
  --region=${REGION} \
  --project=${PROJECT_ID}
```

(Or simpler: `gcloud run deploy <service-name> --source=. --region=...` and let Cloud Build do it.)

## Pointing the phone at this gateway

After the first deploy, get the URL:

```
terraform -chdir=iac/terraform output -raw gateway_url
# e.g. https://drone-sensor-dev-gateway-xxxxxxx-uc.a.run.app
```

In the Android app, set:

- `DEFAULT_GRPC_HOST` = `drone-sensor-dev-gateway-xxxxxxx-uc.a.run.app`
- `DEFAULT_GRPC_PORT` = `443`
- `DEFAULT_TLS` = `true`

(Cloud Run terminates TLS at the edge and forwards HTTP/2 cleartext to the container on 50051.)

## Tearing down

```
terraform destroy
```

Note: in `prod` environment Firestore has `delete_protection_state = "DELETE_PROTECTION_ENABLED"` and must be manually disabled before destroy.

## Notes / known gaps

- This skeleton allows **unauthenticated** Cloud Run invocations. mTLS / OIDC device auth lands in Session 5.
- Inference workers and TAK publisher are placeholders (service accounts only). Code arrives in Sessions 3-4.
- `allow_unauthenticated_invocations=false` (Session 5) will require Android client to send `Authorization: Bearer <id_token>` and use proper SA-issued identity tokens.
