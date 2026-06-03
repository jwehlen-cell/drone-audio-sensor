#!/usr/bin/env bash
set -euo pipefail

REPO_URL=$(curl -fsSL -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/REPO_URL)
GCP_PROJECT=$(curl -fsSL -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/GCP_PROJECT)
MOCK_TAK_PORT=$(curl -fsSL -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/MOCK_TAK_PORT)

INSTALL_DIR=/opt/drone-audio-sensor
VENV=${INSTALL_DIR}/.venv

apt-get update
apt-get install -y --no-install-recommends \
  git python3 python3-venv python3-pip

useradd --system --home /var/lib/mock-tak --create-home --shell /usr/sbin/nologin \
  mock-tak || true

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}"
fi
chown -R mock-tak:mock-tak "${INSTALL_DIR}"

sudo -u mock-tak python3 -m venv "${VENV}"
sudo -u mock-tak "${VENV}/bin/pip" install --quiet --upgrade pip
# Receiver itself only needs the Python stdlib; google-cloud-firestore
# powers the optional --firestore persistence sink.
sudo -u mock-tak "${VENV}/bin/pip" install --quiet google-cloud-firestore

cat >/etc/systemd/system/mock-tak.service <<EOF
[Unit]
Description=Mock TAK receiver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
User=mock-tak
Group=mock-tak
WorkingDirectory=${INSTALL_DIR}
Environment=GOOGLE_CLOUD_PROJECT=${GCP_PROJECT}
Environment=MOCK_TAK_PORT=${MOCK_TAK_PORT}
ExecStart=${VENV}/bin/python ${INSTALL_DIR}/scripts/mock_tak_server/server.py \\
    --port ${MOCK_TAK_PORT} \\
    --collection tak_events \\
    --ttl-seconds 3600
StandardOutput=append:/var/log/mock-tak.log
StandardError=append:/var/log/mock-tak.log

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

touch /var/log/mock-tak.log
chown mock-tak:mock-tak /var/log/mock-tak.log

systemctl daemon-reload
systemctl enable --now mock-tak.service
