#!/usr/bin/env bash
# Pull the latest repo state on the VM and restart the simulator service.
set -euo pipefail

PROJECT=${PROJECT:-drone-audio-sensor}
ZONE=${ZONE:-us-central1-a}
INSTANCE=${INSTANCE:-drone-sim-sender}

gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" --command="
set -e
cd /opt/drone-audio-sensor
sudo -u drone-sim git fetch --depth=1 origin main
sudo -u drone-sim git reset --hard origin/main
sudo -u drone-sim /opt/drone-audio-sensor/.venv/bin/pip install --quiet -r /opt/drone-audio-sensor/scripts/requirements.txt
sudo systemctl restart drone-simulator
systemctl is-active drone-simulator
echo 'updated and restarted'
"
