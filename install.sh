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

if [[ ! -x "$SOURCE_BINARY" ]]; then
    echo "ERROR: Modified InputPlumber binary was not found:"
    echo "$SOURCE_BINARY"
    exit 1
fi

echo "Installing runtime binary to:"
echo "$INSTALLED_BINARY"

sudo mkdir -p "$INSTALL_DIR"
sudo install -m755 "$SOURCE_BINARY" "$INSTALLED_BINARY"

echo "Restarting InputPlumber..."
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

echo
echo "Installation completed successfully."
