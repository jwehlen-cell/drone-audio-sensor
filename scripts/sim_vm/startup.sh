#!/usr/bin/env bash
# Runs on first boot via GCE startup-script metadata. Idempotent: re-running
# pulls the latest repo state and restarts the service. Logs to
# /var/log/drone-sim-startup.log and via journalctl.
set -euo pipefail

LOG=/var/log/drone-sim-startup.log
exec > >(tee -a "$LOG") 2>&1

echo "=== $(date -u) startup-script begin ==="

META=http://metadata.google.internal/computeMetadata/v1/instance/attributes
hdr=(-sH "Metadata-Flavor: Google")
GATEWAY_URL=$(curl "${hdr[@]}" "$META/GATEWAY_URL")
GCP_PROJECT=$(curl "${hdr[@]}" "$META/GCP_PROJECT")
REPO_URL=$(curl "${hdr[@]}" "$META/REPO_URL")
echo "GATEWAY_URL=$GATEWAY_URL"
echo "GCP_PROJECT=$GCP_PROJECT"
echo "REPO_URL=$REPO_URL"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    git ca-certificates build-essential

# Non-root user that owns the install + runs the service.
id drone-sim >/dev/null 2>&1 || \
    useradd --system --create-home --shell /usr/sbin/nologin drone-sim

INSTALL_DIR=/opt/drone-audio-sensor
if [ ! -d "$INSTALL_DIR/.git" ]; then
    rm -rf "$INSTALL_DIR"
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
else
    git -C "$INSTALL_DIR" fetch --depth=1 origin main
    git -C "$INSTALL_DIR" reset --hard origin/main
fi

# Audio fixtures for the simulator's real-WAV streaming mode. The
# saraalemadi DroneAudioDataset is 1 GB total because of its ESC-50
# noise pool, so we sparse-checkout only the two multiclass drone
# folders we actually use (bebop_1 + membo_1, ~40 MB combined).
# drone-visualization is ~53 MB, small enough for a full clone.
FIXTURES_DIR="$INSTALL_DIR/data/sim_audio_fixtures"
mkdir -p "$FIXTURES_DIR"
if [ ! -d "$FIXTURES_DIR/DroneAudioDataset/.git" ]; then
    rm -rf "$FIXTURES_DIR/DroneAudioDataset"
    git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/saraalemadi/DroneAudioDataset.git \
        "$FIXTURES_DIR/DroneAudioDataset"
    git -C "$FIXTURES_DIR/DroneAudioDataset" sparse-checkout set \
        Multiclass_Drone_Audio/bebop_1 \
        Multiclass_Drone_Audio/membo_1
fi
if [ ! -d "$FIXTURES_DIR/drone-visualization/.git" ]; then
    rm -rf "$FIXTURES_DIR/drone-visualization"
    git clone --depth 1 \
        https://github.com/mackenzie-jane/drone-visualization.git \
        "$FIXTURES_DIR/drone-visualization"
fi

chown -R drone-sim:drone-sim "$INSTALL_DIR"

VENV="$INSTALL_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    sudo -u drone-sim python3 -m venv "$VENV"
fi
sudo -u drone-sim "$VENV/bin/pip" install --quiet --upgrade pip
sudo -u drone-sim "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/scripts/requirements.txt"

# Render the systemd unit template.
sed -e "s|@@GATEWAY_URL@@|$GATEWAY_URL|g" \
    -e "s|@@GCP_PROJECT@@|$GCP_PROJECT|g" \
    -e "s|@@INSTALL_DIR@@|$INSTALL_DIR|g" \
    -e "s|@@VENV@@|$VENV|g" \
    "$INSTALL_DIR/scripts/sim_vm/simulator.service.template" \
    > /etc/systemd/system/drone-simulator.service

# Log file owned by drone-sim so the service can append.
touch /var/log/drone-simulator.log
chown drone-sim:drone-sim /var/log/drone-simulator.log

systemctl daemon-reload
systemctl enable drone-simulator.service
systemctl restart drone-simulator.service

echo "=== $(date -u) startup-script end ==="
