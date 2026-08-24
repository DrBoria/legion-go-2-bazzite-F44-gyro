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

# Composite device profile that routes the Legion Go 2 to the 'deck' (SteamDeck)
# target. Without it the default Bazzite profile routes to 'xbox-elite', which
# has NO gyroscope — so the controller must be emulated as a Steam Deck instead.
PROFILE_DIR="/etc/inputplumber/devices.d"
PROFILE_FILE="$PROFILE_DIR/50-legion_go_2.yaml"
SOURCE_PROFILE="$BASE_DIR/50-legion_go_2.yaml"

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

if [[ -f "$SOURCE_PROFILE" ]]; then
    echo "Installing composite device profile (Legion Go 2 -> 'deck' target, gyro-capable)..."
    sudo mkdir -p "$PROFILE_DIR"
    sudo install -m644 "$SOURCE_PROFILE" "$PROFILE_FILE"
else
    echo "WARNING: 50-legion_go_2.yaml not found next to install.sh."
    echo "         The 'deck' (Steam Deck) routing was NOT installed —"
    echo "         the controller may fall back to xbox-elite (no gyro)."
fi

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

echo "Installing suspend/resume power fix (sleep + Steam re-detect after wake)..."

# The virtual Steam Deck controller is attached over vhci_hcd/usbip. An active
# usbip connection makes the kernel refuse suspend (vhci_hcd: We have 1 active
# connection. Do not suspend.) -> instant wake. inputplumber-suspend.service
# (WantedBy=sleep.target) drops the connection on sleep (ExecStart=HookSleep) and
# re-creates it on wake (ExecStop=HookWake).
#
# On resume HookWake re-creates the controller (28de:1205) but does NOT re-trigger
# udev, so Steam (which ran through suspend) never re-detects it. This drop-in
# overrides the wake side (ExecStop only) to also force a udev re-scan of the
# input/hidraw/iio nodes -> the controller returns to Steam after every wake.
# The suspend side (ExecStart/HookSleep) is left untouched.
SUSPEND_DROPIN_DIR="/etc/systemd/system/inputplumber-suspend.service.d"
SUSPEND_DROPIN_FILE="$SUSPEND_DROPIN_DIR/resume-fix.conf"

sudo mkdir -p "$SUSPEND_DROPIN_DIR"
sudo tee "$SUSPEND_DROPIN_FILE" >/dev/null <<SYSTEMD
[Service]
ExecStop=
ExecStop=/bin/bash -c 'busctl call org.shadowblip.InputPlumber /org/shadowblip/InputPlumber/Manager org.shadowblip.InputManager HookWake; sleep 2; udevadm trigger --subsystem-match=input --subsystem-match=hidraw --subsystem-match=iio'
SYSTEMD

sudo systemctl daemon-reload
sudo systemctl enable --now inputplumber-suspend.service

echo
echo "Service status: $(systemctl is-active inputplumber)"
echo "Suspend hook (sleep fix): $(systemctl is-enabled inputplumber-suspend.service) / $(systemctl is-active inputplumber-suspend.service)"
echo "Resume fix (Steam re-detect): $SUSPEND_DROPIN_FILE"

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

echo "Composite device profile: $PROFILE_FILE"

echo
echo "Installation completed successfully."
