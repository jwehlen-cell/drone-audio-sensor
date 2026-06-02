#!/usr/bin/env bash
# Tear down the Argos UAT pull bridge VM.
set -euo pipefail

PROJECT=${PROJECT:-argosuat}
ZONE=${ZONE:-us-west2-a}
INSTANCE=${INSTANCE:-argos-bridge}

echo "Deleting $INSTANCE in $ZONE..."
gcloud compute instances delete "$INSTANCE" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --quiet
echo "Done."
