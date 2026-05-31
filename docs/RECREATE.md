# Recreate runbook — bootstrap a new GCP account/project

This runbook bootstraps the drone-audio-sensor stack into a **fresh GCP
project** using the state captured during the 2026-05-29 teardown of
the original `drone-audio-sensor` project.

The original project's billable resources are fully torn down. The
captured state is preserved locally under
`teardown_export_20260529_210509/` (gitignored). Everything that isn't
in that directory is reproducible from git.

## What was preserved

```
teardown_export_20260529_210509/
├── 00_pre_destroy_inventory.txt         # full gcloud listing as of 21:05 UTC
├── 01_terraform.tfstate.before_destroy  # Terraform state snapshot (75 resources)
├── 01_terraform.tfstate.backup.before_destroy
├── 02_drone-sim-sender.instance.json    # hand-managed simulator VM definition
├── firestore/
│   ├── devices.jsonl                    # 12 fake phone registrations
│   ├── devices/<ID>.json                # per-device export, 1 file each
│   ├── detections.jsonl                 # 422 historical detection events
│   └── detections/<ID>.json
└── secrets/
    └── README.md                        # the 2 secrets were empty containers
```

Also preserved in the repo (not the export dir):

- `.simulator-keys/DRONE-SENSOR-NNN.{private,public}.pem` — 8 device
  key pairs (gitignored via `*.pem`; you must back these up out-of-band
  if you don't have the laptop). Each public key was registered in
  Firestore against the matching `device_id` — that mapping is in
  `firestore/devices.jsonl`.
- `iac/terraform/` — the source of truth for 74 of the 75 resources.
- The 4 backend services (gateway/inference/admin/tak_publisher),
  Dockerfiles, Cloud Build configs.

## Recreate path

### 0. Pre-reqs

- Have the `teardown_export_20260529_210509/` directory accessible
  (defaults to inside this repo, gitignored).
- Have `.simulator-keys/` accessible (8 `*.private.pem` + `*.public.pem`).
- New GCP project ID picked, billing account linked.
- Local: `gcloud`, `terraform`, `docker` (for builds), Python 3.13 venv
  per `scripts/.venv` (used by Firestore re-import).

### 1. Point gcloud + Terraform at the new project

```bash
export NEW_PROJECT=<your-new-project-id>
gcloud config set project "$NEW_PROJECT"
gcloud auth application-default set-quota-project "$NEW_PROJECT"

# Copy/edit terraform.tfvars from the example
cp iac/terraform/terraform.tfvars.example iac/terraform/terraform.tfvars
# At minimum, set project_id = "$NEW_PROJECT". Keep region/zone defaults
# unless moving region. Leave image vars empty for the first apply — the
# Cloud Run services come up on the "hello" placeholder and get rolled
# forward in step 3.
```

### 2. Bring up the IaC baseline

```bash
cd iac/terraform
terraform init
terraform apply -var project_id="$NEW_PROJECT"
```

Brings up: VPC + private subnet + VPC connector, private-service peering,
Redis, Firestore (empty), Pub/Sub topics + subs, Artifact Registry
repo, 4 service accounts + IAM, monitoring alerts, log bucket, and 4
Cloud Run services pointing at the placeholder `hello` image.

### 3. Build + deploy the 4 service images

```bash
cd $(git rev-parse --show-toplevel)
make deploy-gateway        # builds, pushes, rolls Cloud Run
make deploy-inference      # ~10-15 min the first time (TF + TF Hub layer)
make deploy-tak-publisher
make deploy-admin          # also runs terraform apply with admin_image=<sha>
```

The inference container bakes in the trained YAMNet heads from
`backend/inference/models/` (the 6-class characterizer committed as
`0a56145`). No model retraining needed.

### 4. Re-import Firestore data

The Terraform `google_firestore_database.default` resource creates an
empty `(default)` database. Re-hydrate it from the export:

```bash
NEW_PROJECT="$NEW_PROJECT" \
EXPORT_DIR=teardown_export_20260529_210509 \
scripts/.venv/bin/python scripts/import_firestore_export.py
```

The import script (added below) reads `firestore/devices.jsonl` +
`firestore/detections.jsonl`, decodes the `__type__` sentinels
(timestamp, geopoint, ref, bytes) back into native Firestore types,
and writes each doc by `__id__` into the named collection.

### 5. Re-create the simulator VM

The simulator VM was hand-managed (not in Terraform). Re-create from
the captured instance definition:

```bash
# Quick path: re-run the bootstrap script that produced the original VM
# (commit 6788b58 documents it under scripts/sim_vm/).
scripts/sim_vm/bootstrap.sh                  # adjust project to $NEW_PROJECT first

# Or hand-create matching the captured spec:
# teardown_export_*/02_drone-sim-sender.instance.json has the full
# machineType, disk image, metadata, network tags, startup-script.
```

The simulator inside the VM authenticates as each `DRONE-SENSOR-NNN`
using its private key from `.simulator-keys/`; the corresponding public
key is in Firestore against the device document.

### 6. Populate the (empty) Secret Manager containers

The 2 secrets were empty in the original project. Recreate after the
new TAK endpoint + bootstrap material are known:

```bash
echo -n '<bootstrap_token_blob>' | gcloud secrets versions add \
    drone-sensor-dev-device-bootstrap --data-file=-
echo -n '<tak_credentials_blob>' | gcloud secrets versions add \
    drone-sensor-dev-tak-credentials --data-file=-
```

### 7. Smoke test

```bash
# Inference worker should log subtype_labels=[bebop;mambo;matrice;mavic3;mavicmini;no_drone]
gcloud logging read \
  'resource.type="cloud_run_revision" jsonPayload.event="yamnet_loaded"' \
  --limit=1 --format='value(jsonPayload.subtype_labels)'

# Admin UI loads (allUsers invoker is on by default in dev tfvars)
open "https://$(gcloud run services describe drone-sensor-dev-admin --region=us-central1 --format='value(status.url)')"
```

## What is NOT recreated by this runbook

- **Cloud Logging history** from the old project. New project starts
  with empty logs.
- **Cloud Build history.** Image manifest is captured in
  `00_pre_destroy_inventory.txt` if you need to know which SHA was
  serving what at teardown time.
- **The 4 service images themselves** — they get rebuilt from source
  in step 3. No registry-to-registry copy needed.

## Notes

- The original project shell `drone-audio-sensor` was left empty (not
  deleted) per the teardown plan. If you want it gone too:
  `gcloud projects delete drone-audio-sensor` (30-day grace period).
- The `default` VPC in the original project was left untouched (GCP
  auto-creates it; harmless).

## Gotchas observed during the 2026-05-30 redeploy into `argosuat`

The redeploy into the AFTAC `argosuat` project surfaced three things
worth knowing for next time:

1. **Don't kick off the `inference` image build in parallel with
   `terraform apply`.** The cloudbuild config calls `gcloud run deploy`
   as its last step. That deploy will *create* the `drone-sensor-dev-inference`
   Cloud Run service if Terraform hasn't already, leading to a 409 on
   the Terraform side and a half-configured service (default Compute SA,
   no VPC connector). Order: baseline `terraform apply` first, then
   build images. The Cloud Run service must exist (Terraform-owned)
   before the image build flips its image.

2. **Admin's cloudbuild config has no `_REGION` substitution.**
   It only builds + pushes (no Cloud Run deploy step — admin is fully
   Terraform-managed). Don't pass `_REGION` in the `--substitutions`
   flag for admin or you get `INVALID_ARGUMENT: key "_REGION" in the
   substitution data is not matched in the template`. Use only
   `_TAG=<sha>,_REPO=<repo>` for admin.

3. **AFTAC org has OS Login enforced, gmail accounts can't SSH.**
   `gcloud compute ssh` and `gcloud compute scp` against the sim VM
   require `roles/compute.osLoginExternalUser` on the org. Without
   that, the sim VM is provisionable (startup script runs from VM
   metadata) but unreachable for `scripts/sim_vm/{status,update}.sh`
   and you can't pre-load `.simulator-keys/`. The simulator generates
   fresh keys on first boot and registers them via the admin endpoint,
   so functionally everything works — just with new key material that
   overwrites whatever public keys you re-imported from
   `firestore/devices.jsonl`.

   Workarounds (pick one):
   - Have an aftac org admin grant
     `roles/compute.osLoginExternalUser` on org `537428431344`. Then
     `scripts/sim_vm/{status,update}.sh` work normally.
   - Bake `.simulator-keys/` into the VM via the startup script (e.g.,
     stage the keys in a private GCS bucket, fetch them in
     `startup.sh` after cloning). This preserves identity across
     redeploys without needing SSH.
   - Accept that the simulator re-generates keys on each fresh
     provision. For UAT this is fine.

4. **`tak-publisher` Cloud Run service fails its startup probe until
   you populate the TAK credentials secret.** The container tries to
   read `drone-sensor-dev-tak-credentials` at boot; with no version it
   raises `NotFound`. This is expected per step 6 above. Until you have
   a real TAK endpoint to point at, the service stays in `False` ready
   state and a `terraform apply` will surface the startup-probe
   failure as an error (but other resources still update successfully
   ahead of it).

5. **Cloud Run URL format**: the legacy `<service>-<hash>-<region-code>.a.run.app`
   pattern is what Terraform's `status.url` reports. `gcloud run` may
   also surface the newer `<service>-<projectNumber>.<region>.run.app`
   form. Both resolve to the same revision; use whichever is convenient.
