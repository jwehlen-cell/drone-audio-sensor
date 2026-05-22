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

## Build + deploy the service images

The Cloud Run resources in this Terraform config are deployed against
the `cloudrun/container/hello` placeholder image — Terraform stops
caring about the image once the resource is created (`ignore_changes`
on `template[0].containers[0].image`). To roll real code use the
top-level Makefile, which drives Cloud Build inside GCP:

```bash
# From the repo root
make deploy-admin
make deploy-gateway
make deploy-inference
make deploy-tak-publisher
# or all four in sequence
make deploy-all
```

Each target uploads the repo tarball to Cloud Build, builds the image
in GCP using the matching `cloudbuild/<service>.yaml`, pushes it to
Artifact Registry, and rolls the Cloud Run service to the new tag.
**Nothing builds on the developer machine.**

The Cloud Build IAM bindings (`roles/run.admin`, `roles/artifactregistry.writer`,
`roles/iam.serviceAccountUser` on each runtime SA) are provisioned by
`cloudbuild.tf`, so they're in place after `terraform apply`.

### Going further: auto-deploy on git push

A `google_cloudbuild_trigger` resource can watch your GitHub repo and
fire the same `cloudbuild/*.yaml` configs on every push to `main`.
That requires a one-time GitHub-repo connection in the Cloud Console
(no API for it). The Makefile path stays useful afterwards for ad-hoc
deploys from feature branches.

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
