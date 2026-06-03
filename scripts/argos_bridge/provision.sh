#!/usr/bin/env bash
# Provision the Argos UAT pull bridge VM.
#
# Prereqs (one-time, in argosuat):
#   gcloud iam service-accounts create drone-sensor-dev-argos-bridge --project=argosuat \
#       --display-name "Argos UAT pull bridge"
#   gcloud projects add-iam-policy-binding argosuat \
#       --member=serviceAccount:drone-sensor-dev-argos-bridge@argosuat.iam.gserviceaccount.com \
#       --role=roles/secretmanager.secretAccessor
#
# Cross-project (run by someone with prod argos admin):
#   gcloud storage buckets add-iam-policy-binding \
#       gs://aftac-argos-dataflow-unzipped \
#       --project=argos-487318 \
#       --member=serviceAccount:drone-sensor-dev-argos-bridge@argosuat.iam.gserviceaccount.com \
#       --role=roles/storage.objectViewer
#
# This script is hand-managed; not in terraform.
set -euo pipefail

PROJECT=${PROJECT:-argosuat}
ZONE=${ZONE:-us-west2-a}
INSTANCE=${INSTANCE:-drone-sensor-dev-argos-bridge}
GATEWAY_URL=${GATEWAY_URL:-drone-sensor-dev-gateway-ps5izj4jxq-wl.a.run.app}
REPO_URL=${REPO_URL:-https://github.com/jwehlen-cell/drone-audio-sensor.git}
SA_EMAIL=${SA_EMAIL:-drone-sensor-dev-argos-bridge@argosuat.iam.gserviceaccount.com}

cd "$(dirname "$0")"

echo "Creating ${INSTANCE} in ${ZONE} (project ${PROJECT})..."

gcloud compute instances create "${INSTANCE}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --machine-type=e2-small \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=15GB \
  --boot-disk-type=pd-standard \
  --network=default \
  --service-account="${SA_EMAIL}" \
  --scopes=https://www.googleapis.com/auth/cloud-platform \
  --tags=drone-sensor-dev-argos-bridge \
  --metadata-from-file=startup-script=startup.sh \
  --metadata=GATEWAY_URL="${GATEWAY_URL}",GCP_PROJECT="${PROJECT}",REPO_URL="${REPO_URL}" \
  --shielded-secure-boot \
  --shielded-vtpm \
  --shielded-integrity-monitoring

cat <<EOF

VM created. Startup script installs dependencies and starts the
bridge (~3-5 min for the first boot — grpcio compiles from source on
e2-small).

  Tail startup script:
    gcloud compute ssh ${INSTANCE} --zone=${ZONE} --project=${PROJECT} \\
      --command='sudo journalctl -u google-startup-scripts.service -f'

  Check bridge service:
    gcloud compute ssh ${INSTANCE} --zone=${ZONE} --project=${PROJECT} \\
      --command='systemctl status drone-sensor-dev-argos-bridge; sudo journalctl -u drone-sensor-dev-argos-bridge -n 30'

  Or just run ./scripts/argos_bridge/status.sh
EOF
