#!/usr/bin/env bash
# Provision a free-tier e2-micro GCE VM to host the audio-burst simulator.
# Hand-managed; not in terraform.
set -euo pipefail

PROJECT=${PROJECT:-drone-audio-sensor}
ZONE=${ZONE:-us-central1-a}
INSTANCE=${INSTANCE:-drone-sim-sender}
GATEWAY_URL=${GATEWAY_URL:-drone-sensor-dev-gateway-65av54lbuq-uc.a.run.app}
REPO_URL=${REPO_URL:-https://github.com/jwehlen-cell/drone-audio-sensor.git}

cd "$(dirname "$0")"

echo "Creating ${INSTANCE} in ${ZONE} (project ${PROJECT})..."

gcloud compute instances create "${INSTANCE}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=10GB \
  --boot-disk-type=pd-standard \
  --network=default \
  --no-service-account \
  --no-scopes \
  --tags=drone-sim-sender \
  --metadata-from-file=startup-script=startup.sh \
  --metadata=GATEWAY_URL="${GATEWAY_URL}",GCP_PROJECT="${PROJECT}",REPO_URL="${REPO_URL}" \
  --shielded-secure-boot \
  --shielded-vtpm \
  --shielded-integrity-monitoring

cat <<EOF

VM created. The startup script is now installing dependencies and starting
the simulator (~2-3 min).

  Tail startup script:
    gcloud compute ssh ${INSTANCE} --zone=${ZONE} --project=${PROJECT} \\
      --command='sudo journalctl -u google-startup-scripts.service -f'

  Check simulator service:
    gcloud compute ssh ${INSTANCE} --zone=${ZONE} --project=${PROJECT} \\
      --command='systemctl status drone-simulator; sudo journalctl -u drone-simulator -n 30'

  Or just run ./scripts/sim_vm/status.sh
EOF
