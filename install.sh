#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "ERROR: Run this script as your regular user, not with sudo."
    exit 1
fi

BASE_DIR="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd
)"

SOURCE_BINARY="$BASE_DIR/inputplumber-legiongo2-gyro"

INSTALL_DIR="/opt/inputplumber-legiongo2-runtime"
INSTALLED_BINARY="$INSTALL_DIR/inputplumber-legiongo2-gyro-v4"

OVERRIDE_DIR="/etc/systemd/system/inputplumber.service.d"
OVERRIDE_FILE="$OVERRIDE_DIR/override.conf"

# Comfortable gyro gains, verified on Legion Go 2 / Bazzite (Fedora 44).
# NOTE: the code default is 50.0 when the env var is unset — far too strong —
# so this systemd override is REQUIRED for comfortable play.
GAIN_CENTER="3.0"
GAIN_HANDLE="5"

if [[ ! -x "$SOURCE_BINARY" ]]; then
    echo "ERROR: Modified InputPlumber binary was not found:"
    echo "$SOURCE_BINARY"
    exit 1
fi

echo "Installing runtime binary to:"
echo "$INSTALLED_BINARY"

sudo mkdir -p "$INSTALL_DIR"
sudo install -m755 "$SOURCE_BINARY" "$INSTALLED_BINARY"

echo "Writing systemd override with comfortable gyro gains..."

sudo mkdir -p "$OVERRIDE_DIR"
sudo tee "$OVERRIDE_FILE" >/dev/null <<SYSTEMD
[Service]
ExecStart=
ExecStart=$INSTALLED_BINARY
Environment=IP_GYRO_GAIN_CENTER=$GAIN_CENTER
Environment=IP_GYRO_GAIN_HANDLE=$GAIN_HANDLE
SYSTEMD

echo "Reloading systemd and restarting InputPlumber..."
sudo systemctl daemon-reload
sudo systemctl restart inputplumber

echo
echo "Service status: $(systemctl is-active inputplumber)"

MAIN_PID="$(
    systemctl show \
        --property=MainPID \
        --value \
        inputplumber.service
)"

if [[ "$MAIN_PID" != "0" ]]; then
    echo "Running binary: $(sudo readlink -f "/proc/$MAIN_PID/exe" || true)"
fi

echo "Gains: $(systemctl show inputplumber.service -p Environment --value)"

echo
echo "Installation completed successfully."
