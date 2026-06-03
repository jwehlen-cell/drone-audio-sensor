#!/usr/bin/env bash
set -euo pipefail
PROJECT=${PROJECT:-argosuat}
ZONE=${ZONE:-us-west2-a}
INSTANCE=${INSTANCE:-drone-sensor-dev-mock-tak}

echo "Deleting $INSTANCE in $ZONE..."
gcloud compute instances delete "$INSTANCE" \
    --project="$PROJECT" --zone="$ZONE" --quiet || true

# Leave the SA + firewall rule in place — they're idempotent and cheap.
echo "done"
