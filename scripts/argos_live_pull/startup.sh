#!/usr/bin/env bash
# Startup script for the bridge VM, live-pull variant.
#
# Replaces scripts/argos_bridge/startup.sh as the GCE startup-script
# metadata so the same e2-small VM hosts the Pub/Sub-based live
# subscriber instead of the GCS-walk replay. Both paths share the
# same install dir, user, and venv; only the systemd unit differs.
#
# Why a separate startup.sh (not just edit the bridge one):
#   * The replay path still depends on a cross-project storage IAM
#     grant on gs://aftac-argos-dataflow-unzipped that's not in
#     place. Keeping its startup.sh untouched means once IAM lands
#     we can swap back without restoring deleted code.
#   * The live-pull path uses the argos-bridge@argos-487318 SA via a
#     key file pulled from argosuat Secret Manager, not the bridge
#     VM's own instance SA, so the auth setup is materially
#     different.
#
# To deploy:
#   gcloud compute instances add-metadata drone-sensor-dev-argos-bridge \
#     --project=argosuat --zone=us-west2-a \
#     --metadata-from-file=startup-script=scripts/argos_live_pull/startup.sh
#   gcloud compute instances reset drone-sensor-dev-argos-bridge \
#     --project=argosuat --zone=us-west2-a
set -euo pipefail

REPO_URL=$(curl -fsSL -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/REPO_URL)
GATEWAY_URL=$(curl -fsSL -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/GATEWAY_URL)
GCP_PROJECT=$(curl -fsSL -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/GCP_PROJECT)

INSTALL_DIR=/opt/drone-audio-sensor
VENV=${INSTALL_DIR}/.venv

apt-get update
apt-get install -y --no-install-recommends \
  git python3 python3-venv python3-pip build-essential \
  python3-dev libsndfile1

# Same system user as the replay-bridge install so we can reuse the
# venv + install dir without churn.
useradd --system --home /var/lib/drone-sensor-dev-argos-bridge \
  --create-home --shell /usr/sbin/nologin \
  drone-sensor-dev-argos-bridge || true

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}"
  chown -R drone-sensor-dev-argos-bridge:drone-sensor-dev-argos-bridge "${INSTALL_DIR}"
else
  # Git refuses to operate as root on a dir owned by another user
  # ("dubious ownership"); run git as the owner instead.
  sudo -u drone-sensor-dev-argos-bridge git -C "${INSTALL_DIR}" fetch --depth=1 origin main
  sudo -u drone-sensor-dev-argos-bridge git -C "${INSTALL_DIR}" reset --hard origin/main
fi

sudo -u drone-sensor-dev-argos-bridge python3 -m venv "${VENV}"
sudo -u drone-sensor-dev-argos-bridge "${VENV}/bin/pip" install --quiet --upgrade pip
# requirements.txt already pulls in a protobuf version (via the
# google-cloud-* deps) that the existing grpcio-tools is happy to
# emit gencode for. Don't add an explicit protobuf pin here -- it
# fights google-cloud-firestore's <6.0.0 constraint and aborts pip.
sudo -u drone-sensor-dev-argos-bridge "${VENV}/bin/pip" install --quiet \
  -r "${INSTALL_DIR}/scripts/requirements.txt" \
  google-cloud-pubsub \
  google-cloud-storage \
  google-cloud-secret-manager

# Regenerate protos into scripts/ so subscriber.py can import them.
sudo -u drone-sensor-dev-argos-bridge "${VENV}/bin/python" -m grpc_tools.protoc \
  -I"${INSTALL_DIR}/proto" \
  --python_out="${INSTALL_DIR}/scripts" \
  --grpc_python_out="${INSTALL_DIR}/scripts" \
  "${INSTALL_DIR}/proto/drone_audio.proto"

# Stop + disable the old replay-bridge unit if it's installed. Leaves
# the unit file on disk so a later switch back is one systemctl
# command, but turns off the 403-on-every-30s loop in the meantime.
if [ -f /etc/systemd/system/drone-sensor-dev-argos-bridge.service ]; then
  systemctl disable --now drone-sensor-dev-argos-bridge.service || true
fi

# Install + start the live-pull unit.
sed \
  -e "s#@@INSTALL_DIR@@#${INSTALL_DIR}#g" \
  -e "s#@@VENV@@#${VENV}#g" \
  -e "s#@@GATEWAY_URL@@#${GATEWAY_URL}#g" \
  -e "s#@@GCP_PROJECT@@#${GCP_PROJECT}#g" \
  "${INSTALL_DIR}/scripts/argos_live_pull/live_pull.service.template" \
  > /etc/systemd/system/drone-sensor-dev-argos-live-pull.service

touch /var/log/argos-live-pull.log
chown drone-sensor-dev-argos-bridge:drone-sensor-dev-argos-bridge \
  /var/log/argos-live-pull.log

systemctl daemon-reload
systemctl enable --now drone-sensor-dev-argos-live-pull.service
