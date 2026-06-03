#!/usr/bin/env bash
# Runs on first boot via GCE startup-script metadata. Idempotent: re-running
# pulls the latest repo state and restarts the service. Logs to
# /var/log/drone-sensor-dev-sim-startup.log and via journalctl.
set -euo pipefail

LOG=/var/log/drone-sensor-dev-sim-startup.log
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

# Optional load-test mode. When LOAD_TEST_MODE=true, the simulator
# drives a large fleet across multiple bases at a single cadence + codec
# (vs the default 10-Patrick mixed-cadence fleet). The clip is pulled
# from a GCS object on each boot so the audio source can swap without
# baking it into the repo.
LOAD_TEST_MODE=$(curl "${hdr[@]}" "$META/LOAD_TEST_MODE" 2>/dev/null || echo "false")
LOAD_TEST_BASES=$(curl "${hdr[@]}" "$META/LOAD_TEST_BASES" 2>/dev/null || echo "")
LOAD_TEST_PHONES_PER_BASE=$(curl "${hdr[@]}" "$META/LOAD_TEST_PHONES_PER_BASE" 2>/dev/null || echo "100")
LOAD_TEST_CADENCE_SECONDS=$(curl "${hdr[@]}" "$META/LOAD_TEST_CADENCE_SECONDS" 2>/dev/null || echo "30")
LOAD_TEST_CODEC=$(curl "${hdr[@]}" "$META/LOAD_TEST_CODEC" 2>/dev/null || echo "flac")
LOAD_TEST_CLIP_GCS=$(curl "${hdr[@]}" "$META/LOAD_TEST_CLIP_GCS" 2>/dev/null || echo "")
LOAD_TEST_GROUND_TRUTH_GCS=$(curl "${hdr[@]}" "$META/LOAD_TEST_GROUND_TRUTH_GCS" 2>/dev/null || echo "")
echo "LOAD_TEST_MODE=$LOAD_TEST_MODE"
echo "LOAD_TEST_BASES=$LOAD_TEST_BASES"
echo "LOAD_TEST_PHONES_PER_BASE=$LOAD_TEST_PHONES_PER_BASE"
echo "LOAD_TEST_CADENCE_SECONDS=$LOAD_TEST_CADENCE_SECONDS"
echo "LOAD_TEST_CODEC=$LOAD_TEST_CODEC"
echo "LOAD_TEST_CLIP_GCS=$LOAD_TEST_CLIP_GCS"
echo "LOAD_TEST_GROUND_TRUTH_GCS=$LOAD_TEST_GROUND_TRUTH_GCS"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    git ca-certificates build-essential

# Non-root user that owns the install + runs the service.
id drone-sensor-dev-sim >/dev/null 2>&1 || \
    useradd --system --create-home --shell /usr/sbin/nologin drone-sensor-dev-sim

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

chown -R drone-sensor-dev-sim:drone-sensor-dev-sim "$INSTALL_DIR"

VENV="$INSTALL_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    sudo -u drone-sensor-dev-sim python3 -m venv "$VENV"
fi
sudo -u drone-sensor-dev-sim "$VENV/bin/pip" install --quiet --upgrade pip
sudo -u drone-sensor-dev-sim "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/scripts/requirements.txt"

# Build EXTRA_ARGS based on mode + pre-stage any load-test clip.
EXTRA_ARGS=""
if [ "$LOAD_TEST_MODE" = "true" ]; then
    CLIPS_DIR="$INSTALL_DIR/data/test_clips"
    mkdir -p "$CLIPS_DIR"
    chown drone-sensor-dev-sim:drone-sensor-dev-sim "$CLIPS_DIR"
    if [ -n "$LOAD_TEST_CLIP_GCS" ]; then
        LOCAL_CLIP="$CLIPS_DIR/$(basename "$LOAD_TEST_CLIP_GCS")"
        echo "Downloading load-test clip: $LOAD_TEST_CLIP_GCS -> $LOCAL_CLIP"
        sudo -u drone-sensor-dev-sim gcloud storage cp \
            "$LOAD_TEST_CLIP_GCS" "$LOCAL_CLIP"
        EXTRA_ARGS="$EXTRA_ARGS --clip $LOCAL_CLIP"
    fi
    if [ -n "$LOAD_TEST_GROUND_TRUTH_GCS" ]; then
        LOCAL_GT="$CLIPS_DIR/$(basename "$LOAD_TEST_GROUND_TRUTH_GCS")"
        echo "Downloading load-test ground truth: $LOAD_TEST_GROUND_TRUTH_GCS -> $LOCAL_GT"
        sudo -u drone-sensor-dev-sim gcloud storage cp \
            "$LOAD_TEST_GROUND_TRUTH_GCS" "$LOCAL_GT"
        EXTRA_ARGS="$EXTRA_ARGS --ground-truth $LOCAL_GT"
    fi
    if [ -n "$LOAD_TEST_BASES" ]; then
        EXTRA_ARGS="$EXTRA_ARGS --bases $LOAD_TEST_BASES"
        EXTRA_ARGS="$EXTRA_ARGS --phones-per-base $LOAD_TEST_PHONES_PER_BASE"
        EXTRA_ARGS="$EXTRA_ARGS --cadence-seconds $LOAD_TEST_CADENCE_SECONDS"
        EXTRA_ARGS="$EXTRA_ARGS --codec $LOAD_TEST_CODEC"
    fi
    echo "Load-test EXTRA_ARGS=$EXTRA_ARGS"
fi

# Render the systemd unit template.
sed -e "s|@@GATEWAY_URL@@|$GATEWAY_URL|g" \
    -e "s|@@GCP_PROJECT@@|$GCP_PROJECT|g" \
    -e "s|@@INSTALL_DIR@@|$INSTALL_DIR|g" \
    -e "s|@@VENV@@|$VENV|g" \
    -e "s|@@EXTRA_ARGS@@|$EXTRA_ARGS|g" \
    "$INSTALL_DIR/scripts/sim_vm/simulator.service.template" \
    > /etc/systemd/system/drone-sensor-dev-sim.service

# Log file owned by drone-sensor-dev-sim so the service can append.
touch /var/log/drone-sensor-dev-sim.log
chown drone-sensor-dev-sim:drone-sensor-dev-sim /var/log/drone-sensor-dev-sim.log

systemctl daemon-reload
systemctl enable drone-sensor-dev-sim.service
systemctl restart drone-sensor-dev-sim.service

echo "=== $(date -u) startup-script end ==="
