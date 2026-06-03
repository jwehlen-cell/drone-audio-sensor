#!/usr/bin/env bash
# Provision the mock TAK receiver VM.
#
# Public IP so the TAK publisher Cloud Run service can reach it
# directly without a VPC connector. Locked to TCP port 8089 ingress.
set -euo pipefail

PROJECT=${PROJECT:-argosuat}
ZONE=${ZONE:-us-west2-a}
INSTANCE=${INSTANCE:-drone-sensor-dev-mock-tak}
PORT=${PORT:-8089}
REPO_URL=${REPO_URL:-https://github.com/jwehlen-cell/drone-audio-sensor.git}
SA_EMAIL=${SA_EMAIL:-drone-sensor-dev-mock-tak@argosuat.iam.gserviceaccount.com}

cd "$(dirname "$0")"

# One-time SA creation (idempotent: ignores "already exists").
gcloud iam service-accounts create drone-sensor-dev-mock-tak \
  --project="${PROJECT}" \
  --display-name "Mock TAK receiver" 2>/dev/null || true

# datastore.user for the optional Firestore sink. No other grants needed.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role=roles/datastore.user --condition=None >/dev/null

# One-time firewall rule: allow TAK ingress from anywhere. Sandbox-only;
# tighten to Cloud Run egress prefixes if this ever leaves the UAT tier.
# Firewall rule on drone-sensor-dev-vpc allowing VPC connector subnet
# (10.8.0.0/28) ingress on the TAK port. Required for the Cloud Run
# TAK publisher to reach the receiver via its VPC connector.
gcloud compute firewall-rules create allow-mock-tak-from-connector \
  --project="${PROJECT}" \
  --network=drone-sensor-dev-vpc \
  --direction=INGRESS \
  --action=allow \
  --rules="tcp:${PORT}" \
  --source-ranges=10.8.0.0/28 \
  --target-tags=mock-tak 2>/dev/null || true

echo "Creating ${INSTANCE} in ${ZONE} (project ${PROJECT})..."

gcloud compute instances create "${INSTANCE}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=10GB \
  --boot-disk-type=pd-standard \
  --network=drone-sensor-dev-vpc \
  --subnet=drone-sensor-dev-subnet \
  --service-account="${SA_EMAIL}" \
  --scopes=https://www.googleapis.com/auth/cloud-platform \
  --tags=mock-tak \
  --metadata-from-file=startup-script=startup.sh \
  --metadata=REPO_URL="${REPO_URL}",GCP_PROJECT="${PROJECT}",MOCK_TAK_PORT="${PORT}" \
  --shielded-secure-boot \
  --shielded-vtpm \
  --shielded-integrity-monitoring

EXT_IP=$(gcloud compute instances describe "${INSTANCE}" \
  --project="${PROJECT}" --zone="${ZONE}" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')

cat <<EOF

VM created. Startup script is installing deps and starting the
mock_tak_server systemd unit (~2-3 min for first boot).

  Public IP:       ${EXT_IP}
  Listen port:     ${PORT}

Once it's up, point the TAK publisher at it:

  printf '%s' '{"host":"${EXT_IP}","port":${PORT},"use_tls":false}' | \\
    gcloud secrets versions add drone-sensor-dev-tak-credentials \\
      --project=${PROJECT} --data-file=-

Then bounce the publisher revision so it re-reads the secret:

  gcloud run services update drone-sensor-dev-tak-publisher \\
    --project=${PROJECT} --region=us-west2 \\
    --update-env-vars=TAK_BOUNCE=\$(date +%s)
EOF
