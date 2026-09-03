#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing (two modes):
#   ./install.sh         plain install; also ensures the diagnostic logger is OFF
#   ./install.sh --log   plain install PLUS installs & enables the passive
#                        diagnostic logger (ip-gyro-logger.service ->
#                        /var/log/ip-gyro-logger.log, mirrored to journalctl)
#   -l / --logger        aliases for --log
# Any unknown flag prints usage and exits non-zero.
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: ./install.sh [--log]

Installs the Legion Go 2 gyro patch (modified InputPlumber binary + composite
device profile + gain override + suspend/resume power fix + Steam gyro
auto-reset unit).

  (no flag)   normal install; also disables & removes the diagnostic logger
  --log       normal install PLUS installs & enables the passive diagnostic
              logger (ip-gyro-logger.service) -> /var/log/ip-gyro-logger.log
              (same output also in: journalctl -u ip-gyro-logger)
  -l, --logger  aliases for --log

Run as your regular user — sudo is used internally for the system changes.
EOF
}

LOG_MODE=0
for arg in "$@"; do
    case "$arg" in
        --log|-l|--logger)
            LOG_MODE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown flag: $arg" >&2
            echo
            usage >&2
            exit 1
            ;;
    esac
done

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "ERROR: Run this script as your regular user, not with sudo."
    exit 1
fi

BASE_DIR="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd
)"

# Runtime install paths.
INSTALL_DIR="/opt/inputplumber-legiongo2-runtime"
INSTALLED_BINARY="$INSTALL_DIR/inputplumber-legiongo2-gyro-v4.resume-gamefix"

# Expected sha256 of the CURRENT fix binary — v11 = FIRST binary change since V9:
#  * fix (b) center-gyro fallback — gyro_center proxy/arbiter, BIDIRECTIONAL:
#    if the active center source goes dead the deck's center gyro automatically
#    falls back to the live source (IIO <-> XInput); on a unit whose XInput IMU
#    is dead (SamTsuki case) the live IIO center takes over by itself;
#  * right-handle gyro +15% (RIGHT_GYRO_SCALE 0.15 -> 0.1725); center unchanged.
# Verified after install — a mismatch only warns, never aborts.
EXPECTED_SHA256="553e4967500df1cb06e987e209edd87567c4a555538d5578a1966798372f8d00"

# Resolve which source binary to install, in priority order:
#   1) the fix binary shipped next to install.sh (once added to the repo/tarball)
#   2) the current fix build in the InputPlumber workspace (used while testing)
#   3) the legacy repo binary (kept as a last-resort fallback; sha is verified)
SOURCE_BINARY=""
for cand in \
    "$BASE_DIR/inputplumber-legiongo2-gyro-v4.resume-gamefix" \
    "/home/legion/ip-build/InputPlumber/inputplumber-legiongo2-gyro-v4.resume-gamefix" \
    "$BASE_DIR/inputplumber-legiongo2-gyro"
do
    if [[ -x "$cand" ]]; then
        SOURCE_BINARY="$cand"
        break
    fi
done

# Resolve which logger source directory to install from, in priority order:
#   1) the logger/ dir shipped next to install.sh (once added to the repo/tarball)
#   2) the logger/ dir in the InputPlumber workspace (used while testing)
# Only a dir containing BOTH files qualifies; otherwise the logger is skipped.
SOURCE_LOGGER_DIR=""
for cand in \
    "$BASE_DIR/logger" \
    "/home/legion/ip-build/InputPlumber/logger"
do
    if [[ -f "$cand/ip-gyro-logger.py" ]] && [[ -f "$cand/ip-gyro-logger.service" ]]; then
        SOURCE_LOGGER_DIR="$cand"
        break
    fi
done

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

# Verify the installed binary matches the expected fix build. Warn, do NOT abort.
source_sha="$(sha256sum "$SOURCE_BINARY" | awk '{print $1}')"
if [[ "$source_sha" == "$EXPECTED_SHA256" ]]; then
    echo "Source binary sha256 OK ($source_sha)"
else
    echo "WARNING: source binary sha256 mismatch — this is NOT the current resume-gamefix build:"
    echo "  expected: $EXPECTED_SHA256"
    echo "  actual:   $source_sha"
fi
installed_sha="$(sudo sha256sum "$INSTALLED_BINARY" | awk '{print $1}')"
if [[ "$installed_sha" == "$EXPECTED_SHA256" ]]; then
    echo "Installed binary sha256 OK ($installed_sha)"
else
    echo "WARNING: installed binary sha256 mismatch:"
    echo "  expected: $EXPECTED_SHA256"
    echo "  actual:   $installed_sha"
fi

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

echo "Installing Steam deck controller IMU/gyro auto-reset unit..."

# After a Bazzite (bootc/rpm-ostree) update, Steam can re-register the virtual
# Steam Deck controller WITHOUT initializing its IMU, leaving a stale entry in
# ~/.local/share/Steam/config/virtualgamepadinfo.txt -> dead gyro (sensitivity
# sliders stuck at 0) and the controller shows as "SteamOS Handheld Controller"
# instead of "Steam Deck Controller". This oneshot unit deletes that file
# (idempotent `rm -f`) at boot, before the graphical session / Steam starts, so
# Steam re-registers the controller WITH the IMU initialized on next launch.
#
# This script refuses to run as root (see top), so the invoking user is the real
# desktop user. Use `id -un` (not $USER, which can be stale) to resolve it.
STEAM_USER="$(id -un)"
UNIT_FILE="/etc/systemd/system/steam-deck-uhid-gyro-reset.service"

if [[ -f "$BASE_DIR/steam-deck-uhid-gyro-reset.service" ]]; then
    echo "Writing unit with User=$STEAM_USER and path /home/$STEAM_USER/..."
    sed "s/__STEAM_USER__/$STEAM_USER/g" "$BASE_DIR/steam-deck-uhid-gyro-reset.service" \
        | sudo tee "$UNIT_FILE" >/dev/null
    sudo chmod 644 "$UNIT_FILE"

    sudo systemctl daemon-reload
    sudo systemctl enable --now steam-deck-uhid-gyro-reset.service
else
    echo "WARNING: steam-deck-uhid-gyro-reset.service not found next to install.sh."
    echo "         The boot-time Steam gyro auto-reset was NOT installed."
fi

# ---------------------------------------------------------------------------
# Passive diagnostic logger (two-mode support)
#   --log / -l / --logger  -> install & enable ip-gyro-logger.service
#   plain                  -> disable & remove everything logger-related
# Idempotent: re-running either mode leaves a clean state.
# ---------------------------------------------------------------------------
LOGGER_DIR="/opt/ip-gyro-logger"
LOGGER_UNIT="/etc/systemd/system/ip-gyro-logger.service"
LOGGER_LOG="/var/log/ip-gyro-logger.log"

install_logger() {
    echo
    echo "Installing passive diagnostic logger..."
    if [[ -z "$SOURCE_LOGGER_DIR" ]]; then
        echo "WARNING: logger sources not found in either location:"
        echo "           $BASE_DIR/logger"
        echo "           /home/legion/ip-build/InputPlumber/logger"
        echo "         (ip-gyro-logger.py AND ip-gyro-logger.service are both required.)"
        echo "         The diagnostic logger was NOT installed."
        return 0
    fi
    sudo mkdir -p "$LOGGER_DIR"
    sudo install -m755 "$SOURCE_LOGGER_DIR/ip-gyro-logger.py" "$LOGGER_DIR/ip-gyro-logger.py"
    sudo install -m644 "$SOURCE_LOGGER_DIR/ip-gyro-logger.service" "$LOGGER_UNIT"
    sudo systemctl daemon-reload
    # ALWAYS replace a previously-running logger (its old code stays loaded in
    # memory otherwise): 'enable --now' only starts a stopped unit and does NOT
    # restart an already-active one, so stale logger code would keep running.
    # Enable for persistence, then force a restart of the freshly installed
    # logger so the new code is guaranteed to be what runs -- no manual stop.
    sudo systemctl enable ip-gyro-logger.service
    sudo systemctl restart ip-gyro-logger.service
    echo
    echo "Diagnostic logger enabled (runs as root via ip-gyro-logger.service)."
    echo "  Log file:  $LOGGER_LOG"
    echo "  Journal:   journalctl -u ip-gyro-logger"
}

remove_logger() {
    echo
    echo "Ensuring diagnostic logger is OFF (normal install mode)..."
    sudo systemctl disable --now ip-gyro-logger.service 2>/dev/null || true
    sudo rm -f "$LOGGER_UNIT"
    sudo rm -rf "$LOGGER_DIR"
    sudo systemctl daemon-reload
    sudo rm -f "$LOGGER_LOG"
    echo "  The diagnostic logger is not installed (unit, files and log removed)."
}

if [[ "$LOG_MODE" -eq 1 ]]; then
    install_logger
else
    remove_logger
fi

echo
echo "Service status: $(systemctl is-active inputplumber)"
echo "Suspend hook (sleep fix): $(systemctl is-enabled inputplumber-suspend.service) / $(systemctl is-active inputplumber-suspend.service)"
echo "Resume fix (Steam re-detect): $SUSPEND_DROPIN_FILE"
echo "Auto gyro reset (Steam registry cleared at boot): $(systemctl is-enabled steam-deck-uhid-gyro-reset.service) / $(systemctl is-active steam-deck-uhid-gyro-reset.service)"
echo "Diagnostic logger: $(systemctl is-enabled ip-gyro-logger.service 2>/dev/null || echo 'not installed') / $(systemctl is-active ip-gyro-logger.service 2>/dev/null || echo 'not running')"

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
