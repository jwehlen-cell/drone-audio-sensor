#!/usr/bin/env bash
# Startup script for the Argos UAT pull bridge VM.
# Installs deps, clones the repo, builds the venv, regenerates protos
# from drone_audio.proto, then enables the systemd unit.
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

useradd --system --home /var/lib/argos-bridge --create-home --shell /usr/sbin/nologin \
  argos-bridge || true

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}"
fi
chown -R argos-bridge:argos-bridge "${INSTALL_DIR}"

sudo -u argos-bridge python3 -m venv "${VENV}"
sudo -u argos-bridge "${VENV}/bin/pip" install --quiet --upgrade pip
sudo -u argos-bridge "${VENV}/bin/pip" install --quiet \
  -r "${INSTALL_DIR}/scripts/requirements.txt" \
  google-cloud-storage \
  google-cloud-secret-manager \
  google-cloud-bigquery \
  PyJWT

# Regenerate protos into scripts/ so bridge.py can import them.
sudo -u argos-bridge "${VENV}/bin/python" -m grpc_tools.protoc \
  -I"${INSTALL_DIR}/proto" \
  --python_out="${INSTALL_DIR}/scripts" \
  --grpc_python_out="${INSTALL_DIR}/scripts" \
  "${INSTALL_DIR}/proto/drone_audio.proto"

# Install + start the systemd unit.
sed \
  -e "s#@@INSTALL_DIR@@#${INSTALL_DIR}#g" \
  -e "s#@@VENV@@#${VENV}#g" \
  -e "s#@@GATEWAY_URL@@#${GATEWAY_URL}#g" \
  -e "s#@@GCP_PROJECT@@#${GCP_PROJECT}#g" \
  "${INSTALL_DIR}/scripts/argos_bridge/bridge.service.template" \
  > /etc/systemd/system/argos-bridge.service

touch /var/log/argos-bridge.log
chown argos-bridge:argos-bridge /var/log/argos-bridge.log

systemctl daemon-reload
systemctl enable --now argos-bridge.service
