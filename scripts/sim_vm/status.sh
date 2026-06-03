#!/usr/bin/env bash
# Quick status report for the VM-hosted simulator.
set -euo pipefail

PROJECT=${PROJECT:-drone-audio-sensor}
ZONE=${ZONE:-us-central1-a}
INSTANCE=${INSTANCE:-drone-sensor-dev-sim}

echo "=== VM status ==="
gcloud compute instances describe "$INSTANCE" \
    --project="$PROJECT" --zone="$ZONE" \
    --format='value(status, lastStartTimestamp, machineType.basename())' \
    2>&1 | head -3

echo ""
echo "=== systemd service ==="
gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" \
    --command='systemctl is-active drone-sensor-dev-sim; systemctl status drone-sensor-dev-sim --no-pager -n 5 || true'

echo ""
echo "=== last 20 lines of simulator log ==="
gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" \
    --command='tail -n 20 /var/log/drone-sensor-dev-sim.log 2>/dev/null || echo "(no log yet)"'
