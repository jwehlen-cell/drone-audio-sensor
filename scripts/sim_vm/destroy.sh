#!/usr/bin/env bash
# Tear down the simulator VM.
set -euo pipefail

PROJECT=${PROJECT:-drone-audio-sensor}
ZONE=${ZONE:-us-central1-a}
INSTANCE=${INSTANCE:-drone-sensor-dev-sim}

echo "Deleting $INSTANCE in $ZONE..."
gcloud compute instances delete "$INSTANCE" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --quiet
echo "Done."
