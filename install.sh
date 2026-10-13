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
# Canonical clean name (todo 51f): the stale "-v4" / ".resume-gamefix" fork-label
# suffixes were removed — they carried no meaning and confused.
INSTALLED_BINARY="$INSTALL_DIR/inputplumber-legiongo2"

# Expected sha256 of the CURRENT fix binary — v14 (rebase onto InputPlumber v0.82.0).
#
# v14 = our custom Legion Go 2 gyro work REBASED (merged) onto upstream
# InputPlumber v0.82.0 (previous release was based on v0.79.1). Upstream's
# new source-tracking model (composite_device_sources as (source, serial)
# pairs) and the refresh_event_filter poll-loop hook are adopted; our unique
# logic is kept: gyro calibration (RIGHT_GYRO_SCALE/CENTER_GYRO_SCALE, center
# from right slot), live attach/detach via legion_state, gyro_center
# IIO<->XInput arbiter, composite keep-alive/reconcile + deck-feed re-arm
# (FIX-1/FIX-2), deck-uhid survives suspend, monotonic frame counter,
# publish-settle, LG2 -> Generic 0x12f0 override. Binary sha 258c82b6...
EXPECTED_SHA256="258c82b6a42d2cda84cb1fe20894835a44bd3baeb31867de59c237074f9246fa"
#
# Behaviour preserved from V20 (6b578eb2), which self-healed a physical detach
# on the desktop in <=2s without touching the deck-uhid:
#   1. manager on_source_device_removed: EVERY branch unconditionally cleans
#      source_devices / source_devices_used / composite_device_sources before
#      returning (no stale bookkeeping).
#   2. manager on_source_device_added: a reused-id re-add asks the mapped
#      composite for authoritative membership (HasSourceDevice) and, if the
#      source is no longer live there, treats the mapping as stale, cleans it
#      up and re-attaches the replacement instead of skipping "already in use".
#   3. composite self-heal: when a composite loses its LAST non-blocked
#      gamepad-hidraw source (if02 XInput 61eb), and it persists (keep-alive)
#      and is NOT suspended, it schedules a bounded, debounced reconciliation
#      that rediscovers live hidraw devices and re-adds missing matching ones —
#      the deck-uhid (28de:12f0) is NEVER recreated (the persist-gate is now
#      unconditional; destroy happens only at full teardown).
# persist:true keep-alive (detach), FIX A handle_suspend deck-uhid keep-alive,
# monotonic frame counter and the OGUI gate are UNCHANGED. No IP_ORCHESTRATOR
# env is set any more (the gate no longer exists); startup logs the single
# [single-path] marker.
# Instrumentation: [reconcile] / [mgr-rm] / [mgr-add] / [single-path] markers in
# journalctl. Verified after install — a mismatch only warns, never aborts.
# 2026-09-07 (todo 53a impl): FIX-1/FIX-2 deck-feed re-arm after source re-add/dock
# (steam_deck_uhid.rs poll Empty-branch + always-close config handshake; mod.rs
# on_source_device_added feed-rearm marker + targets.rs rearm_deck_feed helper).
# Rollback: re-run the previous release (v13) tarball's install.sh — it
# restores the v13 (0.79.1-based) binary over the installed one.

# Resolve which source binary to install, in priority order:
#   1) the fix binary shipped next to install.sh (the repo/tarball payload)
#   2) the current fix build in the InputPlumber workspace (used while testing)
SOURCE_BINARY=""
for cand in \
    "$BASE_DIR/inputplumber-legiongo2" \
    "/home/legion/ip-build/InputPlumber/inputplumber-legiongo2"
do
    if [[ -x "$cand" ]]; then
        SOURCE_BINARY="$cand"
        break
    fi
done

# Resolve which logger source directory to install from, in priority order:
#   1) LOGGER_SRC_DIR if the caller exported it (configurable, e.g.
#      LOGGER_SRC_DIR=/path/to/logger ./install.sh --log)
#   2) the logger/ dir shipped next to install.sh (repo default)
#   3) the logger/ dir in the InputPlumber workspace (used while testing)
# The logger is now the v3.7 PACKAGE: the thin launcher `ip-gyro-logger.py` PLUS
# the `ipgyro/` package directory (deployed recursively). A dir qualifies only if
# it contains the launcher, the unit AND the ipgyro package; otherwise the logger
# is skipped (installing just a launcher without ipgyro/ would be broken).
SOURCE_LOGGER_DIR="${LOGGER_SRC_DIR:-}"
if [[ -z "$SOURCE_LOGGER_DIR" ]]; then
    for cand in \
        "$BASE_DIR/logger" \
        "/home/legion/ip-build/InputPlumber/logger"
    do
        if [[ -f "$cand/ip-gyro-logger.py" ]] && \
           [[ -f "$cand/ip-gyro-logger.service" ]] && \
           [[ -d "$cand/ipgyro" ]]; then
            SOURCE_LOGGER_DIR="$cand"
            break
        fi
    done
fi

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
    echo "WARNING: source binary sha256 mismatch — this is NOT the current v14 (0.82.0-based) build:"
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
# A1 — SINGLE CONFIG (todo 52a/52b): one python-free TOML config read by the
# logger via tomllib (stdlib). Installed by install.sh --log in TEMPLATE MODE:
#   * absent  -> fresh copy of the bundle template logger/config.ipgyro.toml
#   * present -> NEVER overwrite user values; only APPEND keys/sections the
#                existing file does not yet define (new tunables surface, the
#                user's overrides are byte-preserved). Invalid/unreadable user
#                config is left untouched (the logger deep-merges built-in
#                defaults, so nothing breaks — new keys just aren't in the file).
LOGGER_CFG_DIR="/etc/ip-gyro-logger"
LOGGER_CFG_FILE="$LOGGER_CFG_DIR/config.toml"
LOGGER_CFG_TEMPLATE="$SOURCE_LOGGER_DIR/config.ipgyro.toml"

install_logger_config() {
    if [[ ! -f "$LOGGER_CFG_TEMPLATE" ]]; then
        echo "WARNING: $LOGGER_CFG_TEMPLATE not found next to logger sources."
        echo "         /etc/ip-gyro-logger/config.toml NOT installed (logger uses built-in defaults)."
        return 0
    fi
    sudo mkdir -p "$LOGGER_CFG_DIR"
    if [[ ! -f "$LOGGER_CFG_FILE" ]]; then
        sudo install -m644 "$LOGGER_CFG_TEMPLATE" "$LOGGER_CFG_FILE"
        echo "  config: $LOGGER_CFG_FILE (installed fresh from template)"
        return 0
    fi
    # Existing config: template-merge ONLY missing keys (never overwrite user
    # values). python3 is already a hard logger requirement; tomllib gives an
    # exact TOML read of both files. If python3 is missing the user config is
    # left untouched — the logger deep-merges built-in defaults, so nothing
    # breaks, the new keys just are not surfaced in the file.
    if command -v python3 >/dev/null 2>&1; then
        local merged
        merged="$(python3 - "$LOGGER_CFG_TEMPLATE" "$LOGGER_CFG_FILE" 2>/dev/null <<'PY'
import sys, tomllib


def fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    return "null"


tpl_path, cfg_path = sys.argv[1], sys.argv[2]
try:
    with open(tpl_path, "rb") as f:
        tpl = tomllib.load(f)
except Exception:
    raise SystemExit(0)  # template unreadable -> leave user config untouched
try:
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
except Exception:
    raise SystemExit(0)  # invalid/unreadable user config -> do not touch it
out = []
for sec, vals in tpl.items():
    if sec not in cfg:
        out.append("")
        out.append("[" + sec + "]")
        for k, v in vals.items():
            out.append("".join([k, " = ", fmt(v)]))
        continue
    for k, v in vals.items():
        if k not in cfg[sec]:
            out.append("")
            out.append("# added by ip-gyro-logger install (new template key, todo 52b)")
            out.append("".join([sec, ".", k, " = ", fmt(v)]))
if not out:
    raise SystemExit(0)
print("\n".join(out))
PY
        )" || true
        if [[ -n "$merged" ]]; then
            # Missing keys are appended as dotted keys at the END, so the user's
            # file body is byte-untouched; dotted keys are valid TOML and
            # tomllib reads the same effective values as if they sat in-section.
            printf '%s\n' "$merged" | sudo tee -a "$LOGGER_CFG_FILE" >/dev/null
            echo "  config: $LOGGER_CFG_FILE exists — appended only missing template keys (user values untouched)"
        else
            echo "  config: $LOGGER_CFG_FILE exists and is up to date (nothing to add)"
        fi
    else
        echo "  config: $LOGGER_CFG_FILE exists — left as-is (no python3 for template merge; logger uses defaults for new keys)"
    fi
}

install_logger() {
    echo
    echo "Installing passive diagnostic logger (v3.7: thin launcher + ipgyro package + config)..."
    if [[ -z "$SOURCE_LOGGER_DIR" ]] || \
       [[ ! -f "$SOURCE_LOGGER_DIR/ip-gyro-logger.py" ]] || \
       [[ ! -d "$SOURCE_LOGGER_DIR/ipgyro" ]]; then
        echo "WARNING: logger v3.7 sources not found:"
        echo "           LOGGER_SRC_DIR=${SOURCE_LOGGER_DIR:-<unset>}"
        echo "           $BASE_DIR/logger   (repo default)"
        echo "           /home/legion/ip-build/InputPlumber/logger"
        echo "         (the thin launcher ip-gyro-logger.py AND the ipgyro/"
        echo "          package directory are both required.)"
        echo "         The diagnostic logger was NOT installed."
        return 0
    fi
    # Non-writing import check (PYTHONDONTWRITEBYTECODE => no .pyc pollution):
    # make sure the v3.7 package actually imports before it goes live.
    if command -v python3 >/dev/null 2>&1; then
        if PYTHONDONTWRITEBYTECODE=1 python3 -c \
            "import sys; sys.path.insert(0, '$SOURCE_LOGGER_DIR'); import ipgyro.app" 2>/dev/null; then
            echo "  logger v3.7 package imports OK ($SOURCE_LOGGER_DIR)"
        else
            echo "WARNING: ipgyro.app did not import from $SOURCE_LOGGER_DIR."
            echo "         The diagnostic logger was NOT installed."
            return 0
        fi
    fi
    sudo mkdir -p "$LOGGER_DIR"
    # Thin launcher first (keeps the exact filename the unit/ExecStart expects).
    sudo install -m755 "$SOURCE_LOGGER_DIR/ip-gyro-logger.py" "$LOGGER_DIR/ip-gyro-logger.py"
    # Then the WHOLE ipgyro/ package, recursively. A stale/older deployment is
    # replaced in full (never merged): the pre-v3.6 MONOLITH had no ipgyro/, and
    # a leftover ipgyro/ would otherwise keep running stale code.
    sudo rm -rf "$LOGGER_DIR/ipgyro"
    sudo mkdir -p "$LOGGER_DIR/ipgyro"
    sudo cp -r "$SOURCE_LOGGER_DIR/ipgyro/." "$LOGGER_DIR/ipgyro/"
    sudo chown -R root:root "$LOGGER_DIR/ipgyro"
    # Do not ship __pycache__ bytecode (regenerated by Python at first run;
    # avoids stale pyc compiled by a different interpreter).
    sudo find "$LOGGER_DIR/ipgyro" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    sudo install -m644 "$SOURCE_LOGGER_DIR/ip-gyro-logger.service" "$LOGGER_UNIT"
    # A1 — install /etc/ip-gyro-logger/config.toml from the bundle template
    # (fresh copy or append-only-missing-keys) BEFORE the unit is (re)started,
    # so the logger reads the config on its first run after the upgrade.
    install_logger_config
    sudo systemctl daemon-reload
    # ALWAYS replace a previously-running logger (its old code stays loaded in
    # memory otherwise): 'enable --now' only starts a stopped unit and does NOT
    # restart an already-active one, so stale logger code would keep running.
    # Enable for persistence, then force a restart of the freshly installed
    # logger so the new code is guaranteed to be what runs -- no manual stop.
    sudo systemctl enable ip-gyro-logger.service
    sudo systemctl restart ip-gyro-logger.service
    echo
    echo "Diagnostic logger v3.7 enabled (runs as root via ip-gyro-logger.service)."
    echo "  Install dir: $LOGGER_DIR (launcher + ipgyro/ package)"
    echo "  Config:      $LOGGER_CFG_FILE"
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

# ---------------------------------------------------------------------------
# Journald retention + logrotate for the diagnostic log (observability).
# Installed ALWAYS (plain AND --log): journald storage is bounded and the
# growing /var/log/ip-gyro-logger.log is rotated, so diagnostic evidence
# survives (design doc MIGRATION.md §8).
# ---------------------------------------------------------------------------
JOURNALD_DROPIN_DIR="/etc/systemd/journald.conf.d"
JOURNALD_DROPIN_FILE="$JOURNALD_DROPIN_DIR/99-ip-gyro-retention.conf"
SOURCE_JOURNALD_DROPIN="$BASE_DIR/journald-99-ip-gyro-retention.conf"
LOGROTATE_FILE="/etc/logrotate.d/ip-gyro-logger"
SOURCE_LOGROTATE="$BASE_DIR/logrotate-ip-gyro-logger"

install_log_retention() {
    echo
    echo "Installing journald retention + logrotate (observability)..."
    if [[ -f "$SOURCE_JOURNALD_DROPIN" ]]; then
        sudo mkdir -p "$JOURNALD_DROPIN_DIR"
        sudo install -m644 "$SOURCE_JOURNALD_DROPIN" "$JOURNALD_DROPIN_FILE"
        echo "  journald retention: $JOURNALD_DROPIN_FILE"
    else
        echo "WARNING: $SOURCE_JOURNALD_DROPIN not found next to install.sh."
        echo "         Journald retention was NOT installed."
    fi
    if [[ -f "$SOURCE_LOGROTATE" ]]; then
        sudo install -m644 "$SOURCE_LOGROTATE" "$LOGROTATE_FILE"
        echo "  logrotate:          $LOGROTATE_FILE"
    else
        echo "WARNING: $SOURCE_LOGROTATE not found next to install.sh."
        echo "         Logrotate for the logger log was NOT installed."
    fi
    # Best-effort apply of the new limits; never abort the install.
    sudo systemctl restart systemd-journald 2>/dev/null || true
    sudo journalctl --vacuum-size=512M 2>/dev/null || true
}

# Counterpart for a future/remove flow: reverts the two files above. This
# install.sh has no uninstall mode, so it is defined for symmetry only and is
# deliberately NOT called here — retention stays installed in every mode.
remove_log_retention() {
    echo
    echo "Removing journald retention + logrotate..."
    sudo rm -f "$JOURNALD_DROPIN_FILE"
    sudo rm -f "$LOGROTATE_FILE"
    sudo systemctl restart systemd-journald 2>/dev/null || true
    echo "  Journald retention and logrotate removed."
}

if [[ "$LOG_MODE" -eq 1 ]]; then
    install_logger
else
    remove_logger
fi

# Observability is installed in BOTH modes (plain install and --log): retention
# must survive regardless of whether the diagnostic logger is enabled.
install_log_retention

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
