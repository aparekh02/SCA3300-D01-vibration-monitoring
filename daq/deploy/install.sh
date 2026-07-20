#!/usr/bin/env bash
# daq/deploy/install.sh
#
# Installs this daq/ tree as a system service on a Raspberry Pi. Run this
# ON THE PI, after uploading the code (see README.md "Deploying to a
# Raspberry Pi" for the upload step) -- it does not copy any code itself.
#
# Assumes the deployment layout the systemd units in this directory
# reference: this script's parent directory (daq/) deployed to /opt/daq.
# Run from wherever you actually uploaded it; if that isn't /opt/daq,
# edit WorkingDirectory/ExecStart in daq-acquire.service to match before
# running this.
#
# Usage:
#   cd /opt/daq/deploy && sudo -E ./install.sh
#
# Idempotent: safe to re-run after an update (e.g. after re-syncing new
# code) to reinstall the venv/units/udev rule.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAQ_DIR="$(dirname "$DEPLOY_DIR")"
SERVICE_USER="${DAQ_SERVICE_USER:-daq}"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo ./install.sh) -- creates a system user, installs udev/systemd units." >&2
    exit 1
fi

echo "Installing daq acquisition service from $DAQ_DIR"

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating system user/group '$SERVICE_USER'"
    useradd --system --user-group --home-dir "$DAQ_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG spi,dialout "$SERVICE_USER" || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$DAQ_DIR"

echo "Creating/updating venv at $DAQ_DIR/venv"
python3 -m venv "$DAQ_DIR/venv"
"$DAQ_DIR/venv/bin/pip" install --upgrade pip -q
"$DAQ_DIR/venv/bin/pip" install -e "$DAQ_DIR" -q

echo "Installing udev rule"
cp "$DEPLOY_DIR/99-daq-hardware.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger

echo "Installing systemd units"
cp "$DEPLOY_DIR/daq-can0-up.service" /etc/systemd/system/
cp "$DEPLOY_DIR/daq-acquire.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now daq-can0-up.service
systemctl enable --now daq-acquire.service

cat <<EOF

Done.

  Check status : systemctl status daq-acquire.service
  Tail logs    : journalctl -u daq-acquire.service -f
  Restart      : systemctl restart daq-acquire.service

Before relying on this, run the Task 1 verification tools and the
tests/hardware/ suite once (see README.md / HARDWARE_TESTING.md) --
this script installs the service, it does not validate the hardware.
EOF
