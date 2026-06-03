#!/usr/bin/env bash
# Pull the latest repo state on the VM and restart the simulator service.
set -euo pipefail

PROJECT=${PROJECT:-drone-audio-sensor}
ZONE=${ZONE:-us-central1-a}
INSTANCE=${INSTANCE:-drone-sensor-dev-sim}

gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" --command="
set -e
cd /opt/drone-audio-sensor
sudo -u drone-sensor-dev-sim git fetch --depth=1 origin main
sudo -u drone-sensor-dev-sim git reset --hard origin/main
sudo -u drone-sensor-dev-sim /opt/drone-audio-sensor/.venv/bin/pip install --quiet -r /opt/drone-audio-sensor/scripts/requirements.txt
sudo systemctl restart drone-sensor-dev-sim
systemctl is-active drone-sensor-dev-sim
echo 'updated and restarted'
"
