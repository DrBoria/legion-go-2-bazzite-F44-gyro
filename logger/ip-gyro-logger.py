#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ip-gyro-logger.py — passive diagnostic logger for the Legion Go 2 gyro patch.

WHY
---
On the Legion Go 2 (Bazzite) in gaming mode the virtual Steam Deck controller
sometimes maps only A/B (no joysticks, no gyro). This logger passively records
timestamped, greppable evidence on BOTH the desktop and the gaming session so
that, after the fact, we can tell whether:

  * physical input reaches the kernel            -> "EV <dev> KEY/ABS ..." lines
  * the virtual Steam Deck device is present     -> "=== SNAPSHOT ===" lines
  * the IMU produces data                        -> "IIO <dev> gyro=... accel=..." lines
  * InputPlumber / gamescope / sessions change   -> "STARTUP" / "SESSION" lines
  * devices are (re)created on mode switches     -> "UDEV DEVADD/DEVREM" lines

v2 (what v1 could NOT see — where the gaming-mode chain actually breaks):
  * raw reports from the PHYSICAL Legion source -> "HID LEGION-SRC@1.2 ..." lines
    (17EF:61EB hidraw: the input InputPlumber reads from the controller)
  * raw reports INTO the virtual deck controller -> "HIDFLOW DECK-GAME ..." lines
    (28DE:12F0 gaming / 28DE:12FB stale / 28DE:1205 desktop: what Steam really
    receives, incl. decoded gyro pitch/yaw/roll at bytes 30-35)
  * per-second liveness + FLOW STOP/RESUME        -> "HIDFLOW / FLOW" lines
    (the exact second a source goes quiet while its device is still present)
  * mode / attach-detach transitions             -> "STATE mode=..." lines
    (desktop <-> gaming switch, controller attach/detach, extra kernel X-Box pad)
  * InputPlumber's own journal                   -> "IPJ:" lines
    (source hidraw opens, gyro calibration, attach/errors)
  * Steam's virtual-gamepad registry + log       -> "STEAM:" lines
    (did Steam register the FULL 12f0 controller with IMU, or a stale 12fb)

v3 (the full end-to-end picture — WHERE a byte actually gets lost):
  * every 64-byte Steam Controller input report INTO the deck is DECODED per
    report, not just counted -> "DECODE"/"MOTION" lines: named buttons
    (a/x/b/y, shoulders/triggers, dpad, L3/R3/L4/R4/R5, menu/steam/view,
    trackpad/stick touch), stick+trigger axes, and the IMU bytes (accel
    24-29, gyro 30-35) that prove whether gyro motion PHYSICALLY reaches
    Steam during a "dead" window
  * per-direction liveness correlated once per second -> "ACTIVITY" lines
    (EV events | LEGION-SRC reads/s | each DECK read/s + MOTION y/n) so the
    exact direction that broke is visible on ONE line
  * loud loss markers -> "FLOW GAP" (deck stream silent while the physical
    source keeps delivering, frame length != 64, protocol mismatch),
    "GENRESET" (deck report stream regenerated) and "FRAMEJUMP" (skipped
    frames between reads)

v3.1 (Steam Input activation evidence — direction D, the Steam->game hop):
  * Steam's per-app controller UI log is tailed -> "STEAM UI:" lines
    ("Loaded Config ... App ID <game> ..." + focus window/game AppID events),
    i.e. WHICH game Steam Input is actually configured for right now
  * a compact focus-transition marker -> "STEAM UI FOCUS: ..." lines
    (game window vs Steam client UI), logged only when the focused AppID changes
  * running game processes are read from /proc/<pid>/environ (SteamAppId) ->
    "STEAM PROC:" lines proving the game for that AppID is REALLY running
  Together with the existing DECK-GAME stream + ACTIVITY correlation this closes
  the blind spot between "Steam read the reports" and "the game received input".

v3.2 (the physical-source IMU + burst capture — closing the LEFT side of the loop):
  * the PHYSICAL Legion XInput stream (17EF:61EB hidraw, iface 2) is DECODED per
    report like the deck -> "DECODE LEGION-SRC ... IMU-LEGION" lines with the
    big-endian i16 gyro bytes (left_gyro_x/y/z @ 41/43/45, right_gyro_y/x/z @
    54/56/58) that prove gyro motion leaves the controller, not just its header
  * full 64-byte raw hex of the Legion source is captured -> "LEGION-SRC raw=..."
    lines (periodically every 5 s, on the IMU frames, and on protocol anomaly),
    reviving the previously dead "last_hex" sample state
  * every DECK DECODE/MOTION line now also carries raw24-35 (accel+gyro bytes)
    so the deck's own gyro report is correlated byte-for-byte with the source
  * DECK-GAME reads are coalesced: the deck emits ~240-250 frame/s vs ~20 reads/s,
    so as soon as a frame shows IMU motion the rest of the burst is drained and
    processed together instead of being lost between reads
  * IIO sampling enters a fast mode (0.1 s) while a gyro blip is present, so raw
    gyro/accel values line up with the hidraw bursts for correlation; the quiet
    default stays at 1 sample/s

v3.3 (capture EVERY field of the joystick path — the agreed "all" definition):
  * the PHYSICAL Legion XInput frame (0x04 @ byte 0) is decoded into NAMED
    fields for EVERY byte of the 64-byte frame: header/enums/batteries/state
    (0-13), sticks (14-17), every named bit of button bytes 18-21, analog +
    digital triggers and mouse wheel (22-25), touch (26-29), low-quality gyro +
    IMU timestamps (30-34), both IMU accel+gyro i16 BE (35-59: left_accel
    35/37/39, left_gyro 41/43/45, right_accel y/x/z 48/50/52, right_gyro y/x/z
    54/56/58) and the 60-63 trailer
  * a full-field "DECODE ... XFULL ..." line fires on ANY change (gyro motion
    OR a button/stick/trigger/state change) — throttled to ~10 lines/s during
    motion bursts — plus one decoded keepalive per 5 s on the same cadence as
    the raw-hex snapshot, so at rest every field still appears as a NAMED value
    at least every 5 s, never only as an anonymous byte in a raw snapshot

Runs as root via ip-gyro-logger.service so it can read /dev/input/* and
/sys/bus/iio. Pure Python 3 standard library — no dependencies, no rebuild.

Every line is timestamped with local time, written to BOTH the log file and
stdout (`journalctl -u ip-gyro-logger`), and flushed immediately. The logger is
robust: one bad device never crashes the service, and each subsystem re-scans
periodically so devices that come/go with mode switches are picked up.

Usage:
    python3 ip-gyro-logger.py [--log /var/log/ip-gyro-logger.log]
"""

import argparse
import datetime
import hashlib
import os
import platform
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time

PROG = "ip-gyro-logger"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EV_KEY = 0x01
EV_ABS = 0x03

# struct input_event, native layout: 2x signed long long (sec, usec),
# unsigned short (type), unsigned short (code), signed int (value) -> 24 bytes.
EVENT_SIZE = struct.calcsize("<2qHHi")

# Human-readable names for the gamepad codes we care about. Unknown codes are
# printed as 0xNNN / 0xNN so logs stay greppable and complete.
KEY_NAMES = {
    0x105: "KEY_LEFT", 0x106: "KEY_RIGHT", 0x107: "KEY_UP", 0x108: "KEY_DOWN",
    0x11c: "KEY_ENTER",
    0x120: "BTN_TRIGGER", 0x121: "BTN_THUMB", 0x122: "BTN_THUMB2",
    0x128: "BTN_TOP", 0x129: "BTN_TOP2", 0x12a: "BTN_PINKIE", 0x12b: "BTN_BASE",
    0x130: "BTN_SOUTH", 0x131: "BTN_EAST", 0x132: "BTN_NORTH", 0x133: "BTN_WEST",
    0x134: "BTN_TL", 0x135: "BTN_TR", 0x136: "BTN_TL2", 0x137: "BTN_TR2",
    0x138: "BTN_SELECT", 0x139: "BTN_START", 0x13a: "BTN_MODE",
    0x13b: "BTN_THUMBL", 0x13c: "BTN_THUMBR",
    0x220: "BTN_DPAD_UP", 0x221: "BTN_DPAD_DOWN", 0x222: "BTN_DPAD_LEFT",
    0x223: "BTN_DPAD_RIGHT",
    0x2c0: "BTN_DPAD_UP_2", 0x2c1: "BTN_DPAD_DOWN_2",
    0x2c2: "BTN_DPAD_LEFT_2", 0x2c3: "BTN_DPAD_RIGHT_2",
}
ABS_NAMES = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z",
    0x03: "ABS_RX", 0x04: "ABS_RY", 0x05: "ABS_RZ",
    0x06: "ABS_THROTTLE", 0x07: "ABS_RUDDER", 0x08: "ABS_WHEEL",
    0x09: "ABS_GAS", 0x0a: "ABS_BRAKE",
    0x10: "ABS_HAT0X", 0x11: "ABS_HAT0Y",
    0x18: "ABS_HAT1X", 0x19: "ABS_HAT1Y",
}

# Common gamepad button/axis bit numbers (linux/input-event-codes.h).
GAMEPAD_KEY_BITS = (
    0x130, 0x131, 0x132, 0x133, 0x134, 0x135, 0x136, 0x137,
    0x138, 0x139, 0x13a, 0x13b, 0x13c, 0x120, 0x129, 0x12a,
    0x220, 0x221, 0x222, 0x223,
)
GAMEPAD_ABS_BITS = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11)

# Devices we specifically care about even when bitmap detection is odd.
NAME_GAMEPAD_HINTS = (
    "steam deck", "xbox", "controller", "legion", "gamepad",
    "joystick", "vhci", "steamdeck",
)

# Throttle intervals (seconds).
SNAP_INTERVAL = 3.0         # re-read /proc/bus/input/devices
HB_INTERVAL = 15.0          # compact heartbeat when the device set did not change
IIO_INTERVAL = 1.0          # sample gyro/accel
IIO_IDLE_HB = 5.0           # one idle heartbeat per device when magnitude == 0
# v3.2 — fast IIO sampling while the IMU is actually moving (burst capture).
# A first gyro blip arms a fast window; each new blip re-arms it. The quiet
# default behaviour stays at 1 sample/s.
IIO_FAST_INTERVAL = 0.1     # fast cadence while motion is present (10 Hz)
IIO_FAST_HOLD = 2.0         # stay fast this long after the last motion blip
SESS_INTERVAL = 5.0         # loginctl session scan
IIO_RESCAN_INTERVAL = 60.0  # rediscover iio devices (IMU can come/go)
EV_THROTTLE = 0.1           # ~10 EV_ABS lines/sec/device
EV_OPEN_ERR_HB = 60.0       # don't spam "cannot open /dev/input/eventX" errors

IIO_DIR = "/sys/bus/iio/devices"
IIO_RAW_GYRO_RE = re.compile(r"^in_(anglvel|gyro)(_[xyz])?_raw$")
IIO_RAW_ACCEL_RE = re.compile(r"^in_accel(_[xyz])?_raw$")

# v2 — new capture channels and their throttle cadences
HID_FLOW_INTERVAL = 1.0     # per-device hidraw liveness summary (once/sec)
HID_RAW_SAMPLE = 5.0        # full-report hex sample cadence per hidraw
FLOW_STOP_AFTER = 2.0       # silence after data -> log FLOW STOP
FLOW_NEVER_AFTER = 10.0     # attached but never delivered -> FLOW STOP
FLOW_STOP_HB = 20.0         # re-assert a long-standing FLOW STOP only this often
STEAM_SCAN_INTERVAL = 5.0   # Steam registry / controller-log diff cadence
STEAM_TAIL_BYTES = 262144   # max controller.txt bytes considered per scan
IPJ_BACKLOG_LINES = 150     # journalctl -u inputplumber -n backlog at startup
IPJ_MAX_LINES_S = 25        # InputPlumber journal flood gate (lines/sec)
IPJ_RE = re.compile(
    r"(attach|detach|disconnect|connect|composite|source|target|grab|release|"
    r"hidraw|gyro|calib|CEN-|12f0|12fb|1205|61eb|17ef|28de|deck|uhid|vhci|"
    r"error|fail|panic|warn|exception|open|close|spawn|kill|session)",
    re.IGNORECASE,
)
CTL_RE = re.compile(
    r"(12f0|12fb|28de|1205|steam deck|steam controller|imu|gyro|haptic|"
    r"register|error|fail)", re.IGNORECASE)

# v3.1 — Steam Input activation evidence (direction D). Steam's per-app
# controller_ui.txt tells us WHICH app Steam Input is configured for (per-game
# "Loaded Config ... App ID N", the focused game/client window AppID, controller
# connect + ProductID/Serial 12f0), i.e. whether Steam Input really activated on
# the launched game. Running game processes (SteamAppId in /proc environ) close
# the loop. Captured tail-only, heavily filtered, capped per scan.
STEAM_UI_TAIL_BYTES = 262144
STEAM_UI_MAX_LINES = 8
STEAM_UI_FOCUS_RE = re.compile(
    r"OnFocusWindowChanged to (game window type|window type):?.*?\bAppID ([0-9]+)",
    re.IGNORECASE)
# coarse candidate set: per-game config loads, focus events, controller identity
STEAM_UI_CAND_RE = re.compile(
    r"(OnFocusWindowChanged|Controller [0-9]+ (connected|disconnected|attributes)|"
    r"ProductID|Serial|Custom SDL Mapping|Loaded Config)", re.IGNORECASE)
# background config loads that fire for EVERY controller / client UI / desktop
# shell constantly (chord/basicui/desktop) — the per-GAME selection is the real
# signal, so these are excluded from the raw capture.
STEAM_UI_NOISE_RE = re.compile(
    r"(Last Resort Path|basicui_neptune|chord_neptune|desktop_neptune|"
    r"App ID (769|443510|413080)|AppID (769|443510|413080))", re.IGNORECASE)

HIDRAW_DIR = "/sys/class/hidraw"
# hidraw pids we watch -> human label (physical source + virtual deck devices)
WATCH_PIDS = {
    0x61EB: "LEGION-SRC",   # physical Legion Go 2 composite HID = InputPlumber input
    0x12F0: "DECK-GAME",    # virtual Steam Deck controller, GAMING mode (uhid)
    0x12FB: "DECK-12FB",    # stale 12fb form — Steam maps it to A/B only (dead gyro)
    0x1205: "DECK-DESK",    # virtual Steam Controller, DESKTOP mode (vhci)
}
DECK_PIDS = (0x12F0, 0x12FB, 0x1205)
# readlink of /sys/class/hidraw/hidrawN -> ".../0003:VID:PID.XXXX/hidraw/hidrawN"
HIDPATH_RE = re.compile(r"0003:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})")
# USB interface segment inside the sysfs path, e.g. "3-1:1.2/" -> iface 2
USBIFACE_RE = re.compile(r":1\.([0-9]+)/")

# ---------------------------------------------------------------------------
# v3 — per-direction flow correlation / gap-detection cadence
# ---------------------------------------------------------------------------
FLOW_GAP_AFTER = 2.0        # deck silent this long before a FLOW GAP is flagged
FLOW_GAP_HB = 20.0          # re-assert a long-lived FLOW GAP only this often
FLOW_EV_WINDOW = 2.0        # an EV event this recent counts as "user active"
HID_MOTION_LOG_MIN = 0.5    # at most ~2 MOTION lines/sec/deck while moving

# v3.2 — physical Legion-SRC (XInput) decode + raw capture + deck coalescing
LEGO_PID = 0x61EB           # physical Legion Go 2 XInput source (17EF:61EB)
LEGO_MOTION_MIN = 60        # combined gyro magnitude that counts as real motion
LEGO_RAW_HB = 5.0           # periodic full raw-hex cadence for LEGION-SRC
LEGO_MOTION_LOG_MIN = 0.1   # <= ~10 IMU DECODE lines/sec/source while moving
DECK_DRAIN_MAX = 16         # max frames coalesced from one deck burst drain

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

STOP = False                 # set by SIGINT/SIGTERM for a clean exit
LOGFILE = None               # log file handle; None -> stdout only
LOG_LOCK = threading.Lock()  # so the udev thread never interleaves lines

# Session tracking
SESSION_SET = {}
GAMESCOPE_PID = None

# evdev capture state: path -> {"fd", "name", "buf", "last_abs"}
EV_DEVICES = {}
EV_OPEN_ERR = {}

# iio throttle state + discovered device list
IIO_LAST_ACTIVE = {}
IIO_LAST_IDLE = {}
IIO_DEVICES = []
# v3.2 — deadline until which the fast (0.1 s) IIO sampling window stays armed
IIO_FAST_UNTIL = 0.0        # monotonic; extended by every gyro blip

# v2 — hidraw / Steam / InputPlumber journal state
HID_DEVICES = {}            # hidraw node -> {fd,path,label,vid,pid,iface,...}
HID_OPEN_ERR = {}           # /dev/hidrawN -> last open-error time (anti-spam)
STEAM_CACHE = {}            # steam file path -> {reg_sig / ctl_pos / ...}
IPJ_FLOOD = [0.0, 0]        # InputPlumber journal flood gate [window, lines]
LAST_STATE = None           # last computed mode/attach signature (for diffs)

# v3.1 — Steam Input activation on the running game (direction D)
STEAM_UI_FOCUS = None       # last focused AppID label logged (change-only)
STEAM_APP_SUMMARY = None    # last "running Steam games" summary (change-only)
STEAM_PROC_SET = {}         # pid -> (appid, name, cmd) of running Steam games

# v3 — physical-input activity signal (fed by _process_event; reset in the
# per-second HIDFLOW pass). Drives the FLOW GAP "deck silent while active"
# marker so the loss point is visible even without decoding the source proto.
EV_ACT_COUNT = 0            # EV KEY/ABS events since the last ACTIVITY line
EV_ACT_LAST = 0.0           # monotonic time of the most recent EV event

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    """Timestamped (local time) line written to BOTH stdout (-> journald) and
    the log file. Never raises, always flushed."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts} {msg}"
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass
        if LOGFILE is not None:
            try:
                LOGFILE.write(line + "\n")
                LOGFILE.flush()
            except Exception:
                pass

def _signal_handler(signum, _frame):
    """Convert SIGINT/SIGTERM into a clean shutdown instead of an abrupt exit."""
    global STOP
    if not STOP:
        log(f"LOGGER: received signal {signum}, shutting down cleanly")
    STOP = True

def run_cmd(cmd, timeout=5):
    """Run a command; return stripped stdout ('' on failure / not-found)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "").strip()
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# Startup info
# ---------------------------------------------------------------------------

def _log_startup_info():
    """Log OS/host info, inputplumber service state and its cmdline."""
    un = platform.uname()
    log(f"LOGGER: uname system={un.system} release={un.release} "
        f"version={un.version} machine={un.machine}")
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    log(f"LOGGER: os-release {line.split('=', 1)[1].strip().strip(chr(34))}")
                    break
    except Exception:
        pass
    active = run_cmd(["systemctl", "is-active", "inputplumber"])
    log(f"LOGGER: systemctl is-active inputplumber = {active or 'unknown'}")
    pid = run_cmd(["pgrep", "-n", "inputplumber"])
    if pid:
        cmdline = None
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            pass
        log(f"LOGGER: inputplumber pid={pid} cmdline={cmdline or '(unreadable)'}")
    else:
        log("LOGGER: inputplumber process NOT running")

# ---------------------------------------------------------------------------
# /proc/bus/input/devices parsing
# ---------------------------------------------------------------------------

def parse_proc_input_devices():
    """Return a list of device dicts parsed from /proc/bus/input/devices."""
    devices = []
    try:
        with open("/proc/bus/input/devices") as f:
            lines = f.read().splitlines()
    except Exception as e:
        log(f"LOGGER: cannot read /proc/bus/input/devices: {e}")
        return devices

    cur = None
    for line in lines:
        if not line.strip():
            if cur:
                devices.append(cur)
                cur = None
            continue
        if line[0] in "INPSUHB":
            val = line[2:].strip()
            if cur is None:
                cur = {"name": "", "phys": "", "sysfs": "", "handlers": [], "bmap": {}}
            # /proc/bus/input/devices lines carry the field name inside the value,
            # e.g. 'N: Name="Power Button"', 'P: Phys=...', 'H: Handlers=kbd event0'.
            if line[0] == "N":
                cur["name"] = val.partition("=")[2].strip().strip('"')
            elif line[0] == "P":
                cur["phys"] = val.partition("=")[2].strip()
            elif line[0] == "S":
                cur["sysfs"] = val.partition("=")[2].strip()
            elif line[0] == "U":
                cur["uniq"] = val.partition("=")[2].strip()
            elif line[0] == "H":
                cur["handlers"] = val.partition("=")[2].split()
            elif line[0] == "B":
                field, _, rest = val.partition("=")
                cur["bmap"][field] = rest
    if cur:
        devices.append(cur)
    return devices

def _bitmap_has(bmap, field, bit):
    """Test bit `bit` in a /proc/bus/input/devices hex bitmap like KEY=70000 0 ..."""
    raw = bmap.get(field)
    if not raw:
        return False
    words = raw.split()
    w = bit // 32
    if w >= len(words):
        return False
    try:
        return (int(words[w], 16) >> (bit % 32)) & 1
    except ValueError:
        return False

def is_gamepad(dev):
    """Heuristic: a device is gamepad-like if it has a jsN handler, OR it exposes
    EV_KEY+EV_ABS with gamepad button+axis bits, OR its name matches a hint."""
    handlers = dev.get("handlers") or []
    if any(h.startswith("js") for h in handlers):
        return True
    b = dev.get("bmap") or {}
    has_key = _bitmap_has(b, "EV", EV_KEY)
    has_abs = _bitmap_has(b, "EV", EV_ABS)
    if (has_key and has_abs
            and any(_bitmap_has(b, "KEY", k) for k in GAMEPAD_KEY_BITS)
            and any(_bitmap_has(b, "ABS", a) for a in GAMEPAD_ABS_BITS)):
        return True
    name = (dev.get("name") or "").lower()
    return any(tok in name for tok in NAME_GAMEPAD_HINTS)

def device_set_id(devices):
    """Stable identity of the device SET (name+phys+sysfs). Changes only when a
    device appears/disappears — NOT when eventN handlers are renumbered."""
    items = sorted(
        (d.get("name") or "", d.get("phys") or "", d.get("sysfs") or "")
        for d in devices
    )
    return hashlib.md5(repr(items).encode("utf-8", "replace")).hexdigest()

def log_snapshot(devices, full=True):
    """Log a full snapshot (device set changed) or a compact heartbeat line."""
    try:
        if full:
            log("=== SNAPSHOT (device set changed) ===")
            for d in sorted(devices, key=lambda x: (x.get("sysfs") or "", x.get("name") or "")):
                tag = "*" if is_gamepad(d) else " "
                log("  [{}] name={} phys={} handlers={} sysfs={}".format(
                    tag, d.get("name"), d.get("phys"),
                    " ".join(d.get("handlers") or []), d.get("sysfs")))
        gp = sum(1 for d in devices if is_gamepad(d))
        ev = sum(1 for d in devices for h in (d.get("handlers") or []) if h.startswith("event"))
        log(f"HB: devices={len(devices)} gamepads={gp} event_nodes={ev}")
    except Exception as e:
        log(f"LOGGER: snapshot error: {e}")

# ---------------------------------------------------------------------------
# IIO (IMU) gyro/accel sampling
# ---------------------------------------------------------------------------

def discover_iio():
    """Enumerate /sys/bus/iio/devices/iio:device* with gyro/accel raw channels
    and refresh the global IIO_DEVICES list."""
    global IIO_DEVICES
    found = []
    try:
        entries = sorted(os.listdir(IIO_DIR))
    except Exception as e:
        log(f"LOGGER: cannot list {IIO_DIR}: {e}")
        return
    for entry in entries:
        if not entry.startswith("iio:device"):
            continue
        dpath = os.path.join(IIO_DIR, entry)
        name = ""
        try:
            with open(os.path.join(dpath, "name")) as f:
                name = f.read().strip()
        except Exception:
            pass
        try:
            files = os.listdir(dpath)
        except Exception:
            continue
        gyro = sorted(f for f in files if IIO_RAW_GYRO_RE.match(f))
        accel = sorted(f for f in files if IIO_RAW_ACCEL_RE.match(f))
        if not gyro and not accel:
            continue
        scale = ""
        for s in ("in_anglvel_scale", "in_gyro_scale", "in_accel_scale"):
            if os.path.exists(os.path.join(dpath, s)):
                try:
                    with open(os.path.join(dpath, s)) as f:
                        scale = f.read().strip()
                except Exception:
                    pass
                break
        found.append((entry, dpath, name, gyro, accel, scale))

    old_ids = {d[0] for d in IIO_DEVICES}
    new_ids = {e for e, *_ in found}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    if added:
        log(f"IIO: discovered devices: {', '.join(added)}")
    if removed:
        log(f"IIO: devices gone: {', '.join(removed)}")
    IIO_DEVICES = found
    if not IIO_DEVICES:
        log("IIO: no gyro/accel iio devices found (will keep re-checking)")

def _read_axes(paths):
    """Read raw files named *_x_raw/_y_raw/_z_raw (or a single *_raw) and
    return (x, y, z); missing axes are None."""
    vals = {}
    for p in paths:
        try:
            with open(p) as f:
                vals[p] = int(f.read().strip())
        except Exception:
            vals[p] = None

    def pick(axis):
        for p in paths:
            if p.endswith("_%s_raw" % axis):
                return vals.get(p)
        return None

    x, y, z = pick("x"), pick("y"), pick("z")
    if x is None and y is None and z is None and len(paths) == 1:
        return (vals.get(paths[0]), None, None)
    return (x, y, z)

def _fmt(v):
    return "NA" if v is None else str(v)

def _mag(*vals):
    return sum(abs(v) for v in vals if v is not None)

def sample_iio(now):
    """Read every iio gyro/accel device once, log throttled: live when the
    magnitude is non-zero (max ~1 line/device/sec), idle heartbeat every 5s.
    v3.2 — a gyro blip arms the global fast window (IIO_FAST_UNTIL, refreshed on
    each new blip): while armed the main loop samples at IIO_FAST_INTERVAL and
    live lines are logged at that cadence, so raw gyro/accel values line up with
    the hidraw bursts for correlation. The quiet default stays at 1 sample/s."""
    global IIO_FAST_UNTIL
    for entry, dpath, name, gyro, accel, scale in IIO_DEVICES:
        try:
            gx, gy, gz = _read_axes([os.path.join(dpath, f) for f in gyro])
            ax, ay, az = _read_axes([os.path.join(dpath, f) for f in accel])
        except Exception as e:
            log(f"LOGGER: iio read error {entry}: {e}")
            continue
        line = "IIO {}: name={} gyro={},{},{} accel={},{},{}".format(
            entry, name or "?",
            _fmt(gx), _fmt(gy), _fmt(gz),
            _fmt(ax), _fmt(ay), _fmt(az))
        if scale:
            line += f" scale={scale}"
        if _mag(gx, gy, gz, ax, ay, az) > 0:
            # v3.2: real rotation = gyro magnitude (accel always shows gravity),
            # which arms/refreshes the fast sampling window for burst correlation
            if _mag(gx, gy, gz) > 0:
                IIO_FAST_UNTIL = now + IIO_FAST_HOLD
            iv = IIO_FAST_INTERVAL if now < IIO_FAST_UNTIL else IIO_INTERVAL
            if now - IIO_LAST_ACTIVE.get(entry, 0.0) >= iv:
                log(line)
                IIO_LAST_ACTIVE[entry] = now
        else:
            if now - IIO_LAST_IDLE.get(entry, 0.0) >= IIO_IDLE_HB:
                log(line + " (idle)")
                IIO_LAST_IDLE[entry] = now

# ---------------------------------------------------------------------------
# udev monitor (daemon thread, auto-restart)
# ---------------------------------------------------------------------------

UDEVADM_CMD = [
    "udevadm", "monitor", "--udev",
    "--subsystem-match=input", "--subsystem-match=hidraw", "--subsystem-match=iio",
]

def udev_monitor_loop():
    """Watch udev for input/hidraw/iio add/remove events. Restarts udevadm if it
    exits unexpectedly. Runs in a daemon thread."""
    while not STOP:
        proc = None
        try:
            proc = subprocess.Popen(UDEVADM_CMD, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception as e:
            log(f"UDEV: cannot start udevadm monitor: {e} (retrying in 3s)")
            time.sleep(3)
            continue
        log(f"UDEV: udevadm monitor started (pid={proc.pid})")
        try:
            for line in proc.stdout:
                if STOP:
                    break
                line = line.strip()
                if not line:
                    continue
                if " add " in f" {line} ":
                    tag = "DEVADD"
                elif " remove " in f" {line} ":
                    tag = "DEVREM"
                else:
                    tag = "DEVCHG"
                log(f"UDEV {tag}: {line}")
        except Exception as e:
            log(f"UDEV: monitor read error: {e}")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if not STOP:
            log("UDEV: udevadm monitor exited unexpectedly — restarting in 3s")
            time.sleep(3)

# ---------------------------------------------------------------------------
# Session detection (loginctl + gamescope)
# ---------------------------------------------------------------------------

def session_scan():
    """Log session start/stop transitions from loginctl, plus gamescope
    presence as the 'gaming mode' signal."""
    global SESSION_SET, GAMESCOPE_PID
    out = run_cmd(["loginctl", "list-sessions", "--no-legend"]) or ""
    cur = {}
    for line in out.splitlines():
        parts = line.split()
        if parts:
            cur[parts[0]] = line.strip()
    for sid, line in cur.items():
        if sid not in SESSION_SET:
            log(f"SESSION: started {line}")
    for sid in list(SESSION_SET.keys()):
        if sid not in cur:
            log(f"SESSION: ended {SESSION_SET[sid]}")
    SESSION_SET = cur

    gp = run_cmd(["pgrep", "-x", "gamescope"])
    new_pid = gp or None
    if new_pid != GAMESCOPE_PID:
        if new_pid:
            log(f"SESSION: gamescope started (pid={new_pid})")
        else:
            log("SESSION: gamescope stopped")
        GAMESCOPE_PID = new_pid

# ---------------------------------------------------------------------------
# evdev event capture
# ---------------------------------------------------------------------------

def build_node_map(devices):
    """Map eventN handler -> owning device name, so logs show friendly names."""
    m = {}
    for d in devices:
        nm = d.get("name") or ""
        for h in d.get("handlers") or []:
            if h.startswith("event"):
                m[h] = nm
    return m

def scan_evdev(devices, node_map):
    """Open evdev nodes for every gamepad-like device; close nodes that are
    gone. Called every snapshot cycle so devices come/go cleanly."""
    desired = {}
    for d in devices:
        if not is_gamepad(d):
            continue
        for h in d.get("handlers") or []:
            if h.startswith("event"):
                desired["/dev/input/" + h] = node_map.get(h) or d.get("name") or ""

    now = time.monotonic()
    for path, nm in desired.items():
        if path in EV_DEVICES:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            # Permission denied for non-root is expected; log it, don't spam.
            if now - EV_OPEN_ERR.get(path, 0.0) >= EV_OPEN_ERR_HB:
                log(f"EV: cannot open {path}: {e}")
                EV_OPEN_ERR[path] = now
            continue
        EV_DEVICES[path] = {"fd": fd, "name": nm, "buf": bytearray(), "last_abs": 0.0}
        log(f"EV: capturing {path} ({nm or '?'})")

    for path in list(EV_DEVICES.keys()):
        if path not in desired:
            _drop_ev(path)
            log(f"EV: closed {path} (device gone)")

def _drop_ev(path):
    try:
        os.close(EV_DEVICES[path]["fd"])
    except Exception:
        pass
    EV_DEVICES.pop(path, None)

def handle_evdev(now):
    """select() on all open evdev fds and parse any input_event structs."""
    if not EV_DEVICES:
        return
    fds = [e["fd"] for e in EV_DEVICES.values()]
    try:
        ready, _, _ = select.select(fds, [], [], 0.05)
    except InterruptedError:
        return

    for path, entry in list(EV_DEVICES.items()):
        if entry["fd"] not in ready:
            continue
        try:
            chunk = os.read(entry["fd"], 4096)
        except BlockingIOError:
            continue
        except OSError as e:
            log(f"EV: read error on {path}: {e}, dropping device")
            _drop_ev(path)
            continue
        if not chunk:
            log(f"EV: EOF on {path}, dropping device")
            _drop_ev(path)
            continue

        entry["buf"].extend(chunk)
        buf = entry["buf"]
        name = entry.get("name") or path
        while len(buf) >= EVENT_SIZE:
            raw = bytes(buf[:EVENT_SIZE])
            del buf[:EVENT_SIZE]
            try:
                _sec, _usec, etype, code, value = struct.unpack("<2qHHi", raw)
            except struct.error:
                buf.clear()
                break
            _process_event(name, etype, code, value, entry, now)

def _process_event(name, etype, code, value, entry, now):
    """Log EV_KEY presses (value 1) and EV_ABS changes (throttled)."""
    global EV_ACT_COUNT, EV_ACT_LAST
    if etype == EV_KEY:
        if value == 1:
            kname = KEY_NAMES.get(code, f"0x{code:03x}")
            log(f"EV {name} KEY {kname} DOWN")
        EV_ACT_COUNT += 1
        EV_ACT_LAST = now
    elif etype == EV_ABS:
        EV_ACT_COUNT += 1
        EV_ACT_LAST = now
        if now - entry["last_abs"] >= EV_THROTTLE:
            aname = ABS_NAMES.get(code, f"0x{code:02x}")
            log(f"EV {name} ABS {aname} {value}")
            entry["last_abs"] = now
    # EV_SYN (0x00) and everything else are intentionally ignored.

# ---------------------------------------------------------------------------
# v2 — hidraw monitor
# ---------------------------------------------------------------------------
# v1 could only see /dev/input (evdev) + IIO. In GAMING mode the deck
# controller (28DE:12F0, uhid) has NO /dev/input node, so the very reports
# Steam receives were invisible — and so was the raw input InputPlumber reads
# from the physical Legion (17EF:61EB). v2 opens those hidraw nodes directly:
#   LEGION-SRC = physical 17EF:61EB  -> raw reports InputPlumber consumes
#   DECK-GAME  = virtual 28DE:12F0   -> what Steam sees in gaming mode
#   DECK-12FB  = virtual 28DE:12FB   -> stale form (only A/B, no gyro)
#   DECK-DESK  = virtual 28DE:1205   -> what Steam sees on desktop
# hidraw fans each report out to EVERY open reader, so reading alongside
# InputPlumber/Steam is passive — we never steal or acknowledge their reports.

def _readlink(path):
    try:
        return os.readlink(path)
    except Exception:
        return ""

def discover_hidraws():
    """Return watched hidraw entries parsed from /sys/class/hidraw/*."""
    found = {}
    try:
        nodes = sorted(os.listdir(HIDRAW_DIR))
    except Exception as e:
        log(f"HID: cannot list {HIDRAW_DIR}: {e}")
        return found
    for node in nodes:
        if not node.startswith("hidraw"):
            continue
        target = _readlink(os.path.join(HIDRAW_DIR, node))
        m = HIDPATH_RE.search(target)
        if not m:
            continue
        vid = int(m.group(1), 16)
        pid = int(m.group(2), 16)
        if vid not in (0x17EF, 0x28DE) or pid not in WATCH_PIDS:
            continue
        iface = None
        mi = USBIFACE_RE.search(target)
        if mi:
            iface = int(mi.group(1))
        found[node] = {
            "node": node, "path": "/dev/" + node,
            "vid": vid, "pid": pid, "iface": iface,
            "label": WATCH_PIDS[pid],
            "syspath": target.split("/hidraw/")[0] if "/hidraw/" in target else target,
        }
    return found

def _hid_label(e):
    lbl = e["label"]
    if e.get("iface") is not None:
        lbl += f"@1.{e['iface']}"
    return lbl

def _drop_hid(node):
    try:
        os.close(HID_DEVICES[node]["fd"])
    except Exception:
        pass
    HID_DEVICES.pop(node, None)

def scan_hidraw():
    """Open watched hidraw nodes as they appear; close them when gone.
    Returns the current discovery so callers can reuse it (state tracker)."""
    now = time.monotonic()
    found = discover_hidraws()
    for node, info in found.items():
        if node in HID_DEVICES:
            continue
        path = info["path"]
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            if now - HID_OPEN_ERR.get(path, 0.0) >= 30.0:
                log(f"HID: cannot open {path}: {e}")
                HID_OPEN_ERR[path] = now
            continue
        HID_OPEN_ERR.pop(path, None)
        HID_DEVICES[node] = {
            **info, "fd": fd, "buf": bytearray(),
            "last_data": None, "count": 0, "nbytes": 0, "last_len": 0,
            "last_hex": "", "last_dec": {}, "last_raw": 0.0,
            "stopped": False, "stopped_at": 0.0, "never_logged_stop": True,
            "first_attach": now, "frames_seen": {},
            # v3 per-report decode / motion / frame-continuity state
            "dec_count": 0, "mot_count": 0, "mot_last": 0.0, "mot_log": 0.0,
            "btn_prev": "", "last_frame": None,
            "gap_active": False, "gap_at": 0.0,
            "bad_len_logged": False, "bad_hdr_logged": False, "jump_log": 0.0,
            # v3.2 — Legion-SRC decode/raw state + coalesced-deck-burst flag
            "lego_prev": "", "lego_dec_log": 0.0, "lego_hex_log": 0.0,
            "lego_last_motion": False, "lego_bad_logged": False,
            "motion_seen": False,
        }
        log(f"HID: capturing {path} ({_hid_label(HID_DEVICES[node])} "
            f"vid={info['vid']:04X} pid={info['pid']:04X})")
    for node in list(HID_DEVICES.keys()):
        if node not in found:
            log(f"HID: closed /dev/{node} ({_hid_label(HID_DEVICES[node])} gone)")
            _drop_hid(node)
    return found

def _s16(raw, off):
    if off + 2 > len(raw):
        return None
    return struct.unpack_from("<h", raw, off)[0]

def _s16be(raw, off):
    """Big-endian signed i16 (the Legion XInput IMU bytes are MSB-first)."""
    if off + 2 > len(raw):
        return None
    return struct.unpack_from(">h", raw, off)[0]

def _u16(raw, off):
    if off + 2 > len(raw):
        return None
    return struct.unpack_from("<H", raw, off)[0]

def _u16be(raw, off):
    """Big-endian unsigned u16 (Legion XInput touch_x/y are MSB-first)."""
    if off + 2 > len(raw):
        return None
    return struct.unpack_from(">H", raw, off)[0]

def _u32_le(raw, off):
    if off + 4 > len(raw):
        return None
    return struct.unpack_from("<I", raw, off)[0]

# ---------------------------------------------------------------------------
# v3 — full 64-byte Steam Controller input-report decode.
#
# Byte-layout source of truth = InputPlumber's on-wire struct
# `PackedInputDataReport` in src/drivers/steam_deck/hid_report.rs:262-473:
#   #[packed_struct(bit_numbering = "msb0", size_bytes = "64")]
# msb0 numbering over a 64-byte struct => bit 64 is byte 8's MSB, so a
# field's byte-mask is 0x80 >> (bit % 8). Each report is serialized by
# `self.state.pack()` — steam_deck.rs:440 (USB/vhci, DECK-DESK) and
# steam_deck_uhid.rs:107 (UHID, DECK-GAME/12FB). Masks cross-checked against
# SDL_hidapi_steamdeck.c (SteamDeckButtons).
#
# Header: [0]=0x01 major_ver, [1]=0x00 minor_ver, [2]=0x09 report_type,
# [3]=0x40 report_size, [4..7]=u32 LE frame counter.
# ---------------------------------------------------------------------------

# Named buttons by byte index -> (byte-mask, name). Reserved/unnamed bits are
# kept as None and never logged.
DECK_BTN_BYTES = {
    # byte 8, bits 64-71   (hid_report.rs:280-295)
    8: ((0x80, "a"), (0x40, "x"), (0x20, "b"), (0x10, "y"),
        (0x08, "l1"), (0x04, "r1"), (0x02, "l2"), (0x01, "r2")),
    # byte 9, bits 72-79   (hid_report.rs:298-313)
    9: ((0x80, "l5"), (0x40, "menu"), (0x20, "steam"), (0x10, "view"),
        (0x08, "down"), (0x04, "left"), (0x02, "right"), (0x01, "up")),
    # byte 10, bits 80-87  (hid_report.rs:316-331)
    10: ((0x80, None), (0x40, "l3"), (0x20, None), (0x10, "r_pad_touch"),
         (0x08, "l_pad_touch"), (0x04, "r_pad_press"),
         (0x02, "l_pad_press"), (0x01, "r5")),
    # byte 11, bits 88-95  (hid_report.rs:334-349) — only r3 (bit 93 = 0x04)
    11: ((0x80, None), (0x40, None), (0x20, None), (0x10, None),
         (0x08, None), (0x04, "r3"), (0x02, None), (0x01, None)),
    # byte 13, bits 104-111  (hid_report.rs:370-385)
    13: ((0x80, "r_stick_touch"), (0x40, "l_stick_touch"),
         (0x20, None), (0x10, None), (0x08, None),
         (0x04, "r4"), (0x02, "l4"), (0x01, None)),
    # byte 14, bits 112-119  (hid_report.rs:388-403) — quick_access bit 117
    14: ((0x80, None), (0x40, None), (0x20, None), (0x10, None),
         (0x08, None), (0x04, "quick_access"), (0x02, None), (0x01, None)),
}

# stable display/compare order for a pressed-button set
DECK_BTN_ORDER = (
    "a", "b", "x", "y", "l1", "r1", "l2", "r2", "l3", "r3", "l4", "r4",
    "l5", "r5", "steam", "menu", "view", "quick_access",
    "up", "down", "left", "right",
    "l_pad_touch", "r_pad_touch", "l_pad_press", "r_pad_press",
    "l_stick_touch", "r_stick_touch",
)

# deck-protocol signature: byte0 = 0x01 major_ver, byte2 = 0x09 report_type
DECK_HEADER_BYTES = (0x01, 0x09)

# gyro raw-count magnitude below this counts as "quiet" (no real rotation)
DECK_MOTION_MIN = 50

def _deck_buttons(raw):
    """Pressed named buttons in a 64-byte deck report (deterministic order)."""
    if len(raw) < 15:
        return []
    pressed = set()
    for bi, table in DECK_BTN_BYTES.items():
        b = raw[bi]
        for mask, name in table:
            if name and (b & mask):
                pressed.add(name)
    return [n for n in DECK_BTN_ORDER if n in pressed]

def decode_deck_report(raw):
    """Decode one 64-byte virtual-deck input report into a dict, or None when
    the frame does not match the deck protocol (so a 64-byte LEGION-SRC frame
    is never mis-decoded). Axes are unpacked from the bytes cited above:
      sticks  l_stick_x/y = 48-51, r_stick_x/y = 52-55  (i16 LE)
      triggers l_trigg/r_trigg = 44-47                   (u16 LE)
      accel accel_x/y/z = 24-29                          (i16 LE)
      gyro   pitch/yaw/roll = 30-35                      (i16 LE)
    The report's own "pitch/yaw/roll" labels ARE the gyro stream (they map to
    SDL's sGyroX/Y/Z) — that is the MOTION a gyro fix must deliver."""
    if len(raw) < 64:
        return None
    if raw[0] != DECK_HEADER_BYTES[0] or raw[2] != DECK_HEADER_BYTES[1]:
        return None
    return {
        "frame": _u32_le(raw, 4),
        "buttons": _deck_buttons(raw),
        "lsx": _s16(raw, 48), "lsy": _s16(raw, 50),
        "rsx": _s16(raw, 52), "rsy": _s16(raw, 54),
        "lt": _u16(raw, 44), "rt": _u16(raw, 46),
        "ax": _s16(raw, 24), "ay": _s16(raw, 26), "az": _s16(raw, 28),
        "gx": _s16(raw, 30), "gy": _s16(raw, 32), "gz": _s16(raw, 34),
    }

# ---------------------------------------------------------------------------
# v3.3 — full 64-byte PHYSICAL Legion XInput report decode.
#
# Byte-layout source of truth = InputPlumber's on-wire struct `XInputDataReport`
# in src/drivers/lego/hid_report.rs (ReportType::XInputData = 0x04 at byte 0,
# hid_cmd 0x74 = XINPUT_COMMAND_ID at byte 2, #[packed_struct(size_bytes =
# "60")] carried in a 64-byte report). The whole 64-byte frame is decoded into
# NAMED fields (nothing dropped): header/enum/state bytes 0-13, sticks 14-17,
# every named bit of the button bytes 18-21, analog + digital triggers and
# wheel 22-25, touch 26-29, the low-quality gyro bytes + IMU timestamps 30-34,
# accel/gyro i16 (MSB-first) for BOTH IMUs 35-59, and the 60-63 trailer.
#   left_gyro_x/y/z = 41/43/45, right_gyro_y/x/z = 54/56/58  (i16 BE, gyro)
#   left_accel_x/y/z = 35/37/39, right_accel_y/x/z = 48/50/52 (i16 BE, accel)
#   right_accel is stored y@48-49/x@50-51/z@52-53 and right_gyro
#   y@54-55/x@56-57/z@58-59 (Rust field order is NOT x,y,z in the bytes).
# The report's own "left/right gyro" labels ARE the controller's gyro stream —
# the same physical motion the virtual deck later reports as pitch/yaw/roll.
# ---------------------------------------------------------------------------

LEGO_HEADER_BYTES = (0x04, 0x74)

# GamepadMode / ConnectedState value->name maps (mirror hid_report.rs enums).
LEGO_MODE_NAMES = {0x00: "xinput", 0x01: "dinput", 0x02: "fps"}
LEGO_STATE_NAMES = {0x02: "attached", 0x03: "detached"}

def _lego_mode(v):
    """GamepadMode byte 9 -> Display name (mirrors hid_report.rs GamepadMode)."""
    return LEGO_MODE_NAMES.get(v, "unknown")

def _lego_state(v):
    """ConnectedState byte 12/13 -> name (mirrors hid_report.rs ConnectedState:
    0x02 attached, 0x03 detached, anything else -> connecting)."""
    return LEGO_STATE_NAMES.get(v, "connecting")

# Named buttons by byte index -> (byte-mask, name) for the Legion source.
# bytes 18-21 are fully mapped (Rust hid_report.rs:358-428).
LEGO_BTN_BYTES = {
    18: ((0x80, "legion"), (0x40, "quick_access"), (0x20, "thumb_l"),
         (0x10, "thumb_r"), (0x08, "up"), (0x04, "down"),
         (0x02, "left"), (0x01, "right")),
    19: ((0x80, "a"), (0x40, "b"), (0x20, "x"), (0x10, "y"),
         (0x08, "lb"), (0x04, "d_trigger_l"), (0x02, "rb"),
         (0x01, "d_trigger_r")),
    20: ((0x80, "y1"), (0x40, "y2"), (0x20, "y3"), (0x10, "m1"),
         (0x08, "m2"), (0x04, "m3"), (0x02, "view"), (0x01, "menu")),
    21: ((0x80, "mouse_click"), (0x40, "show_desktop"), (0x20, "alt_tab"),
         (0x10, "u21_3"), (0x08, "u21_4"), (0x04, "u21_5"),
         (0x02, "u21_6"), (0x01, "u21_7")),
}

# stable display/compare order for the Legion source button set
LEGO_BTN_ORDER = (
    "legion", "quick_access", "thumb_l", "thumb_r", "up", "down", "left",
    "right", "a", "b", "x", "y", "lb", "rb", "d_trigger_l", "d_trigger_r",
    "y1", "y2", "y3", "m1", "m2", "m3", "view", "menu",
    "mouse_click", "show_desktop", "alt_tab",
    "u21_3", "u21_4", "u21_5", "u21_6", "u21_7",
)

def _lego_buttons(raw):
    """Pressed named buttons in a Legion XInput frame (deterministic order)."""
    if len(raw) < 22:
        return []
    pressed = set()
    for bi, table in LEGO_BTN_BYTES.items():
        b = raw[bi]
        for mask, name in table:
            if name and (b & mask):
                pressed.add(name)
    return [n for n in LEGO_BTN_ORDER if n in pressed]

def decode_lego_report(raw):
    """Decode one 64-byte physical Legion XInput report into a dict, or None
    when the frame is not an XInputData report (a 64-byte DECK frame is never
    mis-decoded). Byte0 must be 0x04 (XInputData report id); when byte2 is 0x74
    (XINPUT_COMMAND_ID) the frame carries the IMU payload (dec["has_imu"]).
    EVERY field of the 64-byte frame is decoded into a named value so nothing
    in the joystick path is left as an anonymous byte:
      header/enums/state: report_id/report_size/hid_cmd, aux bytes 3/4/6/8/10/
        11/24, mode (9), l/r con_state (12/13), batteries (5/7)
      controls: sticks 14-17, button bytes 18-21 (named), analog triggers
        22/23, mouse_z 25, touch_x/y 26-29 (u16 BE)
      IMU: low-quality gyro 30-33, IMU timestamps 34/47, accel+gyro i16 BE for
        left (35-46) and right (48-59)
    All multi-byte IMU/touch fields are big-endian."""
    if len(raw) < 64:
        return None
    if raw[0] != LEGO_HEADER_BYTES[0]:
        return None
    try:
        has_imu = raw[2] == LEGO_HEADER_BYTES[1]
        return {
            "has_imu": has_imu,
            "report_id": raw[0], "report_size": raw[1], "hid_cmd": raw[2],
            "u3": raw[3], "u4": raw[4],
            "lbat": raw[5], "u6": raw[6], "rbat": raw[7], "u8": raw[8],
            "mode": _lego_mode(raw[9]), "u10": raw[10], "u11": raw[11],
            "lst": _lego_state(raw[12]), "rst": _lego_state(raw[13]),
            "lsx": raw[14], "lsy": raw[15], "rsx": raw[16], "rsy": raw[17],
            "buttons": _lego_buttons(raw),
            "alt_l": raw[22], "alt_r": raw[23], "u23": raw[24],
            "mz": raw[25],
            "tx": _u16be(raw, 26), "ty": _u16be(raw, 28),
            "lglqx": raw[30], "lglqy": raw[31],
            "rglqx": raw[32], "rglqy": raw[33],
            "lts": raw[34],
            "lax": _s16be(raw, 35), "lay": _s16be(raw, 37),
            "laz": _s16be(raw, 39),
            "lgx": _s16be(raw, 41), "lgy": _s16be(raw, 43),
            "lgz": _s16be(raw, 45),
            "rts": raw[47],
            "ray": _s16be(raw, 48), "rax": _s16be(raw, 50),
            "raz": _s16be(raw, 52),
            "rgy": _s16be(raw, 54), "rgx": _s16be(raw, 56),
            "rgz": _s16be(raw, 58),
        }
    except IndexError:
        return None

def _lego_fmt(label, dec, raw, tag):
    """Format one full all-fields DECODE line for a Legion XInput frame.
    Keeps the legacy substrings left_gyro=(x=,y=,z=) and right_gyro=(y=,x=,z=)
    so old greps for those tokens still match, and adds every other named
    field + aux bytes + trailer, with the full raw hex at the end."""
    btns = ",".join(dec["buttons"]) or "-"
    return (
        f"DECODE {label} XFULL imu={1 if dec['has_imu'] else 0} "
        f"cmd={dec['hid_cmd']:02x} mode={dec['mode']} "
        f"bat=({dec['lbat']},{dec['rbat']}) state=({dec['lst']},{dec['rst']}) "
        f"sticks=({dec['lsx']},{dec['lsy']},{dec['rsx']},{dec['rsy']}) "
        f"btn=[{btns}] "
        f"trig=({dec['alt_l']},{dec['alt_r']}) wheel={dec['mz']} "
        f"touch=({dec['tx']},{dec['ty']}) "
        f"lqgyro=({dec['lglqx']},{dec['lglqy']},{dec['rglqx']},{dec['rglqy']}) "
        f"ts=({dec['lts']},{dec['rts']}) "
        f"accel_l=({dec['lax']},{dec['lay']},{dec['laz']}) "
        f"left_gyro=(x={dec['lgx']},y={dec['lgy']},z={dec['lgz']}) "
        f"accel_r=({dec['ray']},{dec['rax']},{dec['raz']}) "
        f"right_gyro=(y={dec['rgy']},x={dec['rgx']},z={dec['rgz']}) "
        f"aux=({dec['u3']},{dec['u4']},{dec['u6']},{dec['u8']},"
        f"{dec['u10']},{dec['u11']},{dec['u23']}) "
        f"tr={raw[60:64].hex(' ')} "
        f"raw={raw.hex(' ')} [{tag}]"
    )

def _decode_deck(raw):
    """v2-compatible single-report decode hook (kept for back-compat; the real
    64-byte decode lives in decode_deck_report())."""
    dec = decode_deck_report(raw)
    if dec is None:
        return {}
    return {"pitch": dec["gx"], "yaw": dec["gy"], "roll": dec["gz"]}

def handle_hidraw(now):
    """select() on all open hidraw fds; count reports, keep a raw/decode
    sample, note first-seen frame lengths (format signature), and for the
    virtual-deck pids DECODE every report (buttons/axes/IMU) so a press or a
    gyro blip is visible the instant it reaches Steam."""
    if not HID_DEVICES:
        return
    fds = [e["fd"] for e in HID_DEVICES.values()]
    try:
        ready, _, _ = select.select(fds, [], [], 0.02)
    except InterruptedError:
        return
    for node, e in list(HID_DEVICES.items()):
        if e["fd"] not in ready:
            continue
        try:
            chunk = os.read(e["fd"], 4096)
        except BlockingIOError:
            continue
        except OSError as ex:
            log(f"HID: read error on /dev/{node}: {ex}, dropping")
            _drop_hid(node)
            continue
        if not chunk:
            log(f"HID: EOF on /dev/{node}, dropping")
            _drop_hid(node)
            continue
        t2 = time.monotonic()
        e["count"] += 1
        e["nbytes"] += len(chunk)
        e["last_len"] = len(chunk)
        if e["stopped"]:
            e["stopped"] = False
            log(f"HIDFLOW RESUME {_hid_label(e)} (/dev/{node} delivering data again)")
        # v3: close an open FLOW GAP the moment the deck delivers again
        if e["gap_active"]:
            e["gap_active"] = False
            e["gap_at"] = 0.0
            log(f"FLOW GAP CLOSED {_hid_label(e)} (deck stream delivering again)")
        # first time we see a given frame length, log it (format signature)
        if (len(chunk) <= 128 and len(chunk) not in e["frames_seen"]
                and len(e["frames_seen"]) < 32):
            e["frames_seen"][len(chunk)] = t2
            log(f"HID {_hid_label(e)} FRAME len={len(chunk)} "
                f"head={chunk[:12].hex(' ')}")
        if t2 - e["last_raw"] >= HID_RAW_SAMPLE:
            e["last_raw"] = t2
            e["last_hex"] = chunk[:24].hex(" ")
        # -- v3: decode / inspect every virtual-deck report -------------------
        if e["pid"] in DECK_PIDS:
            _handle_deck_reports(e, chunk, t2)
            # v3.2 — coalesce a motion burst: the deck emits ~240 frame/s but we
            # only read ~20/s, so as soon as the first frame shows IMU motion,
            # drain the rest of the burst NOW (bounded, non-blocking) instead of
            # losing it until the next select tick. Read/liveness counters are
            # still incremented per read, so HIDFLOW/ACTIVITY semantics survive.
            if e["motion_seen"]:
                e["motion_seen"] = False
                for _ in range(DECK_DRAIN_MAX - 1):
                    try:
                        dchunk = os.read(e["fd"], 4096)
                    except BlockingIOError:
                        break
                    except OSError as ex:
                        log(f"HID: read error on /dev/{node} during drain: "
                            f"{ex}, dropping")
                        _drop_hid(node)
                        break
                    if not dchunk:
                        break
                    e["count"] += 1
                    e["nbytes"] += len(dchunk)
                    e["last_len"] = len(dchunk)
                    _handle_deck_reports(e, dchunk, t2)
        # -- v3.2: decode / inspect the PHYSICAL Legion XInput source ---------
        elif e["pid"] == LEGO_PID:
            _handle_lego_reports(e, chunk, t2)
        e["last_data"] = t2

def _handle_deck_reports(e, chunk, t2):
    """Per-report decode + anomaly/gap flags for a virtual-deck hidraw.
    Real deck reports are one 64-byte frame per hidraw read; a read may
    occasionally contain several back-to-back 64-byte frames."""
    label = _hid_label(e)
    L = len(chunk)
    if L and (L == 64 or (L > 64 and L % 64 == 0)):
        reports = [chunk[i:i + 64] for i in range(0, L, 64)]
    else:
        # expected 64 bytes; any other length on a deck pid is an anomaly
        # (flagged only once a normal 64-byte frame has already been seen)
        if L and 64 in e["frames_seen"] and not e["bad_len_logged"]:
            e["bad_len_logged"] = True
            log(f"FLOW GAP {label} anomalous frame len={L} "
                f"(deck reports are 64 bytes) head={chunk[:12].hex(' ')}")
        reports = [] if L == 0 else [chunk]
    for raw in reports:
        dec = decode_deck_report(raw)
        if dec is None:
            if len(raw) == 64 and not e["bad_hdr_logged"]:
                e["bad_hdr_logged"] = True
                log(f"FLOW GAP {label} 64-byte frame with non-deck header "
                    f"head={raw[:12].hex(' ')} (protocol mismatch?)")
            continue
        e["last_dec"] = dec
        e["dec_count"] += 1
        # -- button-set change -> a DECODE line proving a press reached Steam
        sig = ",".join(dec["buttons"])
        if sig != e["btn_prev"]:
            e["btn_prev"] = sig
            log(f"DECODE {label} frame={dec['frame']} btn=[{sig or '-'}] "
                f"ls=({dec['lsx']},{dec['lsy']}) rs=({dec['rsx']},{dec['rsy']}) "
                f"lt={dec['lt']} rt={dec['rt']} "
                f"gyr=({dec['gx']},{dec['gy']},{dec['gz']}) "
                f"raw24-35={raw[24:36].hex(' ')}")
        # -- gyro/accel motion present -> a MOTION line proving IMU reaches deck
        mag = _mag(dec["gx"], dec["gy"], dec["gz"])
        if mag > DECK_MOTION_MIN:
            e["mot_count"] += 1
            e["mot_last"] = t2
            # v3.2: flag the burst so handle_hidraw drains the remaining frames
            e["motion_seen"] = True
            if t2 - e["mot_log"] >= HID_MOTION_LOG_MIN:
                e["mot_log"] = t2
                log(f"MOTION {label} frame={dec['frame']} mag={mag} "
                    f"gyr=({dec['gx']},{dec['gy']},{dec['gz']}) "
                    f"acc=({dec['ax']},{dec['ay']},{dec['az']}) "
                    f"raw24-35={raw[24:36].hex(' ')}")
        # -- frame-counter continuity (bytes 4-7, u32 LE) --------------------
        # Both deck targets increment the counter once per poll() and emit one
        # report per poll, so consecutive reads normally show delta 0 or 1.
        # A backwards jump = new generation; a monotonic jump > 1 = dropped
        # reports (only flagged for the UHID pids, which emit every poll).
        fr = dec["frame"]
        prev = e["last_frame"]
        e["last_frame"] = fr
        if prev is not None and fr is not None and fr != prev:
            if fr < prev:
                if t2 - e["jump_log"] >= 1.0:
                    e["jump_log"] = t2
                    log(f"GENRESET {label} frame {prev} -> {fr} "
                        f"(new generation: counter went backwards)")
            elif fr - prev > 1 and e["pid"] in (0x12F0, 0x12FB):
                if t2 - e["jump_log"] >= 1.0:
                    e["jump_log"] = t2
                    log(f"FRAMEJUMP {label} frame {prev} -> {fr} "
                        f"(skipped {fr - prev - 1} report(s) between reads)")

# ---------------------------------------------------------------------------
# v3.2 — physical Legion XInput source decode + full raw capture
# ---------------------------------------------------------------------------

def _handle_lego_reports(e, chunk, t2):
    """Per-report decode + raw capture for the PHYSICAL Legion XInput source
    (17EF:61EB, iface 2) — the input InputPlumber reads BEFORE the virtual deck.
    Real XInput frames are 64 bytes (report id 0x04 at byte 0); a read may hold
    several back-to-back frames. v3.3 decodes EVERY field of each 64-byte frame
    into named values (see decode_lego_report()) and emits:
      * "LEGION-SRC ... raw=..." full raw hex every LEGO_RAW_HB (5 s);
      * a full "DECODE ... XFULL ..." line whenever ANY field changes (motion
        throttled to LEGO_MOTION_LOG_MIN ~10 lines/s; the first frame of a
        fresh quiet->active edge logs immediately), plus one decoded keepalive
        on the same 5 s cadence as the raw hex so at-rest frames still show
        every field as a NAMED value at least every 5 s."""
    label = _hid_label(e)
    L = len(chunk)
    if L and (L == 64 or (L > 64 and L % 64 == 0)):
        reports = [chunk[i:i + 64] for i in range(0, L, 64)]
    else:
        # expected 64 bytes; any other length on the Legion source is an anomaly
        if L and 64 in e["frames_seen"] and not e["lego_bad_logged"]:
            e["lego_bad_logged"] = True
            log(f"FLOW GAP {label} anomalous frame len={L} "
                f"(legion XInput reports are 64 bytes) head={chunk[:12].hex(' ')}")
        reports = [] if L == 0 else [chunk]
    for raw in reports:
        dec = decode_lego_report(raw)
        if dec is None:
            # 64-byte frame that is not an XInputData report -> protocol mismatch
            if len(raw) == 64 and not e["lego_bad_logged"]:
                e["lego_bad_logged"] = True
                log(f"FLOW GAP {label} 64-byte frame with non-XInput header "
                    f"raw={raw.hex(' ')} (protocol mismatch?)")
            continue
        # -- full raw hex + decoded keepalive every LEGO_RAW_HB ---------------
        if t2 - e["lego_hex_log"] >= LEGO_RAW_HB:
            e["lego_hex_log"] = t2
            log(f"LEGION-SRC {label} raw={raw.hex(' ')}")
            log(_lego_fmt(label, dec, raw, "KEEP"))
        # -- ANY field changed -> full decoded all-fields line -----------------
        # (motion throttled to LEGO_MOTION_LOG_MIN; a fresh quiet->active edge
        # logs its first frame immediately by resetting the throttle stamp)
        if repr(dec) != e["lego_prev"]:
            e["lego_prev"] = repr(dec)
            if dec["has_imu"]:
                mag = _mag(dec["lgx"], dec["lgy"], dec["lgz"],
                           dec["rgx"], dec["rgy"], dec["rgz"])
            else:
                mag = 0
            if mag > LEGO_MOTION_MIN:
                if not e["lego_last_motion"]:
                    e["lego_last_motion"] = True
                    e["lego_dec_log"] = 0.0
                if t2 - e["lego_dec_log"] >= LEGO_MOTION_LOG_MIN:
                    e["lego_dec_log"] = t2
                    log(_lego_fmt(label, dec, raw, "IMU"))
            else:
                e["lego_last_motion"] = False
                # control/state/quiet-field change -> immediate (rare vs motion)
                if t2 - e["lego_dec_log"] >= LEGO_MOTION_LOG_MIN:
                    e["lego_dec_log"] = t2
                    log(_lego_fmt(label, dec, raw, "CHG"))
        else:
            e["lego_last_motion"] = False

def emit_hid_summary(now):
    """Once per second, per watched hidraw:
      - v2 HIDFLOW liveness line (markers/behaviour preserved);
      - v3 FLOW GAP: a GAMING deck that went silent while the physical source
        is still active -> the exact direction/instant input stopped;
      - v3 ACTIVITY: ONE compact cross-direction correlation line
        (EV | LEGION-SRC reads | per-DECK reads + MOTION presence)."""
    global EV_ACT_COUNT
    # -- pre-pass: is the physical source (LEGION-SRC) still delivering? ------
    # (computed before the per-device loop so the result is order-independent)
    src_alive = False
    src_rd = 0
    for e in HID_DEVICES.values():
        if e["vid"] != 0x17EF:
            continue
        src_rd += e["count"]
        if e["count"] or (e["last_data"] is not None
                          and now - e["last_data"] < FLOW_EV_WINDOW):
            src_alive = True
    ev_active = bool(EV_ACT_LAST) and (now - EV_ACT_LAST) <= FLOW_EV_WINDOW
    ev_count = EV_ACT_COUNT
    EV_ACT_COUNT = 0

    deck_parts = []
    for node, e in list(HID_DEVICES.items()):
        label = _hid_label(e)
        reads = e["count"]
        nbytes = e["nbytes"]
        e["count"] = 0
        e["nbytes"] = 0
        # v3 per-second decode counters (consumed here, then reset)
        dec_rd = e["dec_count"]
        mot_rd = e["mot_count"]
        e["dec_count"] = 0
        e["mot_count"] = 0
        if reads:
            dec = e.get("last_dec") or {}
            dstr = mstr = ""
            if e["pid"] in DECK_PIDS and dec:
                dstr = (" gyr(p,y,r)={},{},{}".format(
                    _fmt(dec.get("gx")), _fmt(dec.get("gy")),
                    _fmt(dec.get("gz"))))
                if mot_rd:
                    mstr = " mot={}/{}".format(mot_rd, dec_rd or reads)
            log(f"HIDFLOW {label} {reads} rd/s {nbytes} B/s "
                f"len={e['last_len']}{dstr}{mstr}")
            e["stopped"] = False
            if e["pid"] in DECK_PIDS:
                deck_parts.append((label, reads, mot_rd, dec_rd))
            continue
        # -- no traffic this second -> silence / flow-stop logic --------------
        last = e["last_data"]
        if last is None:
            age = now - e["first_attach"]
            if age >= FLOW_NEVER_AFTER and e["never_logged_stop"]:
                e["never_logged_stop"] = False
                if e["pid"] in (0x12F0, 0x12FB):
                    log(f"FLOW STOP {label} (gaming deck present but NO data "
                        f"since attach -- nothing reaching Steam, "
                        f"{age:.0f}s)")
                else:
                    log(f"FLOW IDLE {label} (event-driven iface or nothing "
                        f"reading it -- informational, {age:.0f}s since attach)")
            # v3 FLOW GAP: gaming deck attached but never delivered while the
            # user is physically active (input blocked before Steam)
            if (e["pid"] in (0x12F0, 0x12FB) and age >= FLOW_NEVER_AFTER
                    and ev_active and not e["gap_active"]):
                e["gap_active"] = True
                e["gap_at"] = now
                log(f"FLOW GAP {label} attached {age:.0f}s, NEVER delivered "
                    f"while PHYSICAL EV ACTIVE -- input reaching Steam "
                    f"blocked here")
            elif (e["pid"] in (0x12F0, 0x12FB) and e["gap_active"]
                    and now - e["gap_at"] >= FLOW_GAP_HB):
                e["gap_at"] = now
                log(f"FLOW GAP {label} (still never delivered "
                    f"{age:.0f}s after attach)")
            if e["pid"] in DECK_PIDS:
                deck_parts.append((label, 0, mot_rd, dec_rd))
            continue
        silent = now - last
        if e["pid"] in DECK_PIDS:
            deck_parts.append((label, 0, mot_rd, dec_rd))
        # -- v3 FLOW GAP: gaming deck quiet while source/EV still active ------
        # This is the loud "data lost HERE" marker: physical side is alive
        # (EV events and/or LEGION-SRC still delivering) but the deck stream
        # that Steam reads has stopped.
        if (e["pid"] in (0x12F0, 0x12FB) and silent >= FLOW_GAP_AFTER
                and (ev_active or src_alive)):
            if not e["gap_active"]:
                e["gap_active"] = True
                e["gap_at"] = now
                log(f"FLOW GAP {label} deck silent {silent:.1f}s while "
                    f"source continues (EV={'active' if ev_active else 'idle'}"
                    f" src={'alive' if src_alive else 'quiet'}) -- input "
                    f"reaching Steam STOPPED here")
            elif now - e["gap_at"] >= FLOW_GAP_HB:
                e["gap_at"] = now
                log(f"FLOW GAP {label} (still silent {silent:.0f}s while "
                    f"source continues)")
        # -- v2 FLOW STOP / IDLE (unchanged behaviour) ------------------------
        if silent >= FLOW_STOP_AFTER and not e["stopped"]:
            e["stopped"] = True
            e["stopped_at"] = now
            log(f"FLOW STOP {label} (no data for {silent:.1f}s, "
                f"device still present)")
        elif e["stopped"] and now - e["stopped_at"] >= FLOW_STOP_HB:
            e["stopped_at"] = now
            log(f"FLOW STOP {label} (still silent, "
                f"{now - last:.0f}s since last data)")

    # -- v3: one compact cross-direction ACTIVITY correlation line ------------
    if HID_DEVICES or ev_count or ev_active:
        act = ["EV={}{}".format(
            ev_count if ev_count < 100 else "100+",
            "!" if ev_active else "")]
        act.append("src={}rd/s{}".format(
            src_rd, "" if src_alive else "(quiet)"))
        for label, reads, mot, dec in deck_parts:
            if reads:
                act.append(f"{label}={reads}rd/s mot={mot}/{dec or reads}")
            else:
                act.append(f"{label}=0rd/s SILENT")
        log("ACTIVITY " + " | ".join(act))

# ---------------------------------------------------------------------------
# v2 — mode / attach-detach state tracker
# ---------------------------------------------------------------------------
# Interprets the union of evdev + hidraw + IIO as a human-readable state:
# which mode we are in (DESKTOP / GAMING / transition), whether the physical
# Legion controller is attached, and whether the extra kernel "Generic X-Box
# pad" (the affected unit's extra non-HID interface) is present.

def _deck_mode(found_hid):
    game = any(h["pid"] in (0x12F0, 0x12FB) for h in found_hid.values())
    desk = any(h["pid"] == 0x1205 for h in found_hid.values())
    if game and desk:
        return "TRANSITION-GAMING+DESKTOP"
    if game:
        return "GAMING"
    if desk:
        return "DESKTOP"
    return "NO-DECK"

def _fmt_state(st):
    return ("mode={} legion_hid=[{}] xpad=[{}] iio={}".format(
        st["mode"], ",".join(st["legion_hid"]) or "-",
        ",".join(st["xpad"]) or "-", st["iio"]))

def state_tracker(devices, found_hid):
    """Diff the derived state against the previous scan and log transitions."""
    global LAST_STATE
    mode = _deck_mode(found_hid)
    legion_hid = sorted(_hid_label(h) for h in found_hid.values()
                        if h["vid"] == 0x17EF)
    xpad = sorted(
        (d.get("name") or "").strip()
        for d in devices if "x-box pad" in (d.get("name") or "").lower())
    st = {"mode": mode, "legion_hid": legion_hid, "xpad": xpad,
          "iio": len(IIO_DEVICES)}
    if LAST_STATE is None:
        log("STATE: baseline " + _fmt_state(st))
        LAST_STATE = st
        return
    if st == LAST_STATE:
        return
    changes = []
    if st["mode"] != LAST_STATE["mode"]:
        changes.append(f"mode {LAST_STATE['mode']} -> {st['mode']}")
    for key, noun in (("legion_hid", "legion hidraw"),
                      ("xpad", "kernel X-Box pad(evdev)")):
        old = set(LAST_STATE[key])
        new = set(st[key])
        if new - old:
            changes.append(f"{noun} attached: {', '.join(sorted(new - old))}")
        if old - new:
            changes.append(f"{noun} detached: {', '.join(sorted(old - new))}")
    if st["iio"] != LAST_STATE["iio"]:
        changes.append(f"iio devices {LAST_STATE['iio']} -> {st['iio']}")
    log("STATE: " + "; ".join(changes) + "  [" + _fmt_state(st) + "]")
    LAST_STATE = st

# ---------------------------------------------------------------------------
# v2 — InputPlumber journal capture (thread + one-shot backlog)
# ---------------------------------------------------------------------------
# Filtered so a single capture shows the InputPlumber side of the chain:
# which hidraw the source opened, gyro calibration, attach/detach, errors.

def _ipj_emit(line):
    wall = time.monotonic()
    if wall - IPJ_FLOOD[0] >= 1.0:
        IPJ_FLOOD[0] = wall
        IPJ_FLOOD[1] = 0
    if IPJ_FLOOD[1] >= IPJ_MAX_LINES_S:
        return
    IPJ_FLOOD[1] += 1
    log("IPJ: " + line.strip())

def ipj_backlog():
    """One-shot recent journal backlog, logged at startup for context."""
    out = run_cmd(["journalctl", "-u", "inputplumber", "-n",
                   str(IPJ_BACKLOG_LINES), "--no-pager", "-o", "short-iso"],
                  timeout=10)
    if not out:
        log("IPJ: no inputplumber journal backlog available")
        return
    log("IPJ: --- backlog (last up to %d inputplumber journal lines) ---"
        % IPJ_BACKLOG_LINES)
    n = 0
    for line in out.splitlines():
        if IPJ_RE.search(line):
            _ipj_emit(line)
            n += 1
    log(f"IPJ: --- end backlog ({n} matching lines) ---")

def ipj_tail_loop():
    """Follow `journalctl -u inputplumber -f`; log filtered lines. Runs in a
    daemon thread and restarts journalctl if it exits unexpectedly."""
    while not STOP:
        proc = None
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", "inputplumber", "-f", "-n", "0",
                 "--no-pager", "-o", "short-iso"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except Exception as e:
            log(f"IPJ: cannot start journalctl: {e} (retrying in 3s)")
            time.sleep(3)
            continue
        log(f"IPJ: journalctl -u inputplumber follow started (pid={proc.pid})")
        try:
            for line in proc.stdout:
                if STOP:
                    break
                if IPJ_RE.search(line):
                    _ipj_emit(line)
        except Exception as e:
            log(f"IPJ: journal read error: {e}")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if not STOP:
            log("IPJ: journalctl exited unexpectedly — restarting in 3s")
            time.sleep(3)

# ---------------------------------------------------------------------------
# v2 — Steam virtual-gamepad registry + controller log
# ---------------------------------------------------------------------------
# Steam can register the deck controller WITHOUT initializing its IMU -> dead
# gyro with the sensitivity sliders stuck at 0. Its decision is recorded in
# ~/.local/share/Steam/config/virtualgamepadinfo.txt (stale entry => 12fb form)
# and its own registration log in ~/.local/share/Steam/logs/controller.txt.

def _steam_bases():
    """Locate desktop-user homes (logger runs as root, Steam data is in /home)."""
    bases = []
    try:
        homes = sorted(os.listdir("/home"))
    except Exception:
        return bases
    for h in homes:
        base = os.path.join("/home", h)
        try:
            st = os.stat(base)
        except Exception:
            continue
        if getattr(st, "st_uid", 0) >= 1000:
            bases.append(base)
    return bases

def steam_scan(now):
    """Diff the Steam registry + controller log + controller_ui (per-app Steam
    Input activation, direction D) for every desktop user, and scan the running
    Steam game processes."""
    bases = _steam_bases()
    if not bases:
        return
    for base in bases:
        _steam_registry(os.path.join(
            base, ".local/share/Steam/config/virtualgamepadinfo.txt"))
        _steam_controller_log(os.path.join(
            base, ".local/share/Steam/logs/controller.txt"))
        _steam_controller_ui_log(os.path.join(
            base, ".local/share/Steam/logs/controller_ui.txt"))
    _steam_procs()

def _steam_registry(path):
    exists = os.path.exists(path)
    sig = None
    content = []
    if exists:
        try:
            with open(path, "r", errors="replace") as f:
                content = f.read().splitlines()
            sig = hashlib.md5(
                "\n".join(content).encode("utf-8", "replace")).hexdigest()
        except Exception as e:
            log(f"STEAM: cannot read registry {path}: {e}")
            return
    cur = STEAM_CACHE.get(path)
    if cur is None:
        cur = {"reg_sig": None}
        STEAM_CACHE[path] = cur
    first = cur["reg_sig"] is None
    if sig == cur["reg_sig"]:
        return
    cur["reg_sig"] = sig
    if first:
        if exists:
            log(f"STEAM: registry present at start "
                f"({len(content)} lines): {path}")
        else:
            log(f"STEAM: registry absent at start (Steam has not registered "
                f"the deck controller yet): {path}")
    elif not exists:
        log(f"STEAM: registry REMOVED (will be re-created on next Steam "
            f"controller registration): {path}")
    if exists:
        # Parse real Steam format: "[slot N]" blocks with key=value lines.
        slots, cur = [], None
        for l in content:
            s = l.strip()
            if not s:
                continue
            if s.startswith("[") and s.endswith("]"):
                cur = {"slot": s, "name": "", "vid": "", "pid": "",
                       "handle": "", "type": ""}
                slots.append(cur)
            elif cur and "=" in s:
                k, _, v = s.partition("=")
                cur[k.strip().lower()] = v.strip()
        deck = [sl for sl in slots
                if sl.get("vid", "").lower().replace("0x", "") == "28de"
                or "deck" in sl.get("name", "").lower()
                or "controller" in sl.get("name", "").lower()]
        pidmap = {
            0x12F0: "FULL gaming deck 12f0 (gyro-capable)",
            0x12FB: "STALE 12fb (A/B only, dead gyro)",
            0x1205: "desktop Steam Controller 1205",
        }
        detail = []
        headline = "no 28de entry"
        for sl in deck:
            ps = sl.get("pid", "")
            try:
                pid_i = int(ps, 16) if ps else 0
            except ValueError:
                pid_i = 0
            verdict = pidmap.get(pid_i, f"pid={ps or '?'}")
            if headline == "no 28de entry":
                headline = verdict
            detail.append(
                f"{sl.get('slot')} name={sl.get('name') or '?'} "
                f"VID={sl.get('vid') or '?'} PID={sl.get('pid') or '?'} "
                f"handle={sl.get('handle') or '?'} type={sl.get('type') or '?'} "
                f"-> {verdict}")
        log(f"STEAM: registry lines={len(content)} verdict={headline} "
            f"28de_slots={len(deck)}")
        for d in detail:
            log(f"STEAM   | {d}")

def _steam_controller_log(path):
    cur = STEAM_CACHE.get(path)
    if cur is None:
        cur = {"ctl_pos": 0, "ctl_present": None}
        STEAM_CACHE[path] = cur
    try:
        size = os.path.getsize(path)
    except OSError:
        if cur.get("ctl_present") is not False:
            cur["ctl_present"] = False
            log(f"STEAM: controller log not present yet: {path}")
        return
    if cur.get("ctl_present") is not True:
        cur["ctl_present"] = True
        cur["ctl_pos"] = size  # start from the end: only NEW lines get logged
        log(f"STEAM: controller log present, size={size} "
            f"(tail-only from now): {path}")
        return
    pos = cur.get("ctl_pos", 0)
    if size < pos:           # log rotated/truncated -> re-read from start
        pos = 0
    if size == pos:
        return
    if size - pos > STEAM_TAIL_BYTES:
        pos = size - STEAM_TAIL_BYTES
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(pos)
            newtext = f.read()
    except Exception:
        return
    cur["ctl_pos"] = size
    shown = 0
    extra = 0
    for line in newtext.splitlines():
        if CTL_RE.search(line):
            if shown < 10:
                log("STEAM controller: " + line.strip())
                shown += 1
            else:
                extra += 1
    if extra:
        log(f"STEAM controller: +{extra} more matching lines this scan")

def _steam_ui_focus(kind, appid):
    """Human-readable focus label for a controller_ui OnFocusWindowChanged."""
    if kind == "game window type":
        return f"focused AppID {appid} (GAME window — Steam Input on the game)"
    if appid == "769":
        return "focused AppID 769 (Steam client UI)"
    if appid == "413080":
        return "focused AppID 413080 (Steam Big Picture / desktop shell)"
    return f"focused AppID {appid} ({kind})"

def _steam_ui_focus_from_text(lines):
    """Most recent OnFocusWindowChanged event in `lines` -> label or None."""
    last = None
    for line in lines:
        m = STEAM_UI_FOCUS_RE.search(line)
        if m:
            last = _steam_ui_focus(m.group(1), m.group(2))
    return last

def _steam_ui_focus_from_tail(path, size):
    """Read the file tail (up to STEAM_UI_TAIL_BYTES) purely for the startup
    baseline focus label."""
    try:
        with open(path, "r", errors="replace") as f:
            if size > STEAM_UI_TAIL_BYTES:
                f.seek(size - STEAM_UI_TAIL_BYTES)
            return _steam_ui_focus_from_text(f.read().splitlines())
    except Exception:
        return None

def _steam_controller_ui_log(path):
    """v3.1 — tail-diff Steam's controller_ui.txt (per-app Steam Input
    activation evidence, direction D). Filtered to per-game config loads, focus
    events and controller identity; logs a compact FOCUS marker on AppID change
    only. Nothing before logger start is replayed."""
    global STEAM_UI_FOCUS
    cur = STEAM_CACHE.get(path)
    if cur is None:
        cur = {"ui_pos": 0, "ui_present": None}
        STEAM_CACHE[path] = cur
    try:
        size = os.path.getsize(path)
    except OSError:
        if cur.get("ui_present") is not False:
            cur["ui_present"] = False
            log(f"STEAM UI: controller_ui log not present yet: {path}")
        return
    if cur.get("ui_present") is not True:
        cur["ui_present"] = True
        cur["ui_pos"] = size   # start from the end: only NEW lines get logged
        log(f"STEAM UI: controller_ui log present, size={size} "
            f"(tail-only from now): {path}")
        focus = _steam_ui_focus_from_tail(path, size)
        if focus:
            STEAM_UI_FOCUS = focus
            log("STEAM UI FOCUS: baseline " + focus)
        return
    pos = cur.get("ui_pos", 0)
    if size < pos:             # rewritten/rotated -> start from scratch
        pos = 0
    if size == pos:
        return
    if size - pos > STEAM_UI_TAIL_BYTES:
        pos = size - STEAM_UI_TAIL_BYTES
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(pos)
            newtext = f.read()
    except Exception:
        return
    cur["ui_pos"] = size
    newlines = newtext.splitlines()
    # -- compact focus transition (only when the focused AppID actually changed)
    focus = _steam_ui_focus_from_text(newlines)
    if focus and focus != STEAM_UI_FOCUS:
        STEAM_UI_FOCUS = focus
        log("STEAM UI FOCUS: " + focus)
    # -- raw filtered evidence lines (bounded, noise excluded) ----------------
    shown = 0
    extra = 0
    for line in newlines:
        if (STEAM_UI_CAND_RE.search(line)
                and not STEAM_UI_NOISE_RE.search(line)):
            if shown < STEAM_UI_MAX_LINES:
                log("STEAM UI: " + line.strip())
                shown += 1
            else:
                extra += 1
    if extra:
        log(f"STEAM UI: +{extra} more matching lines this scan")

def _steam_procs():
    """v3.1 — running game processes launched by Steam, read from the
    SteamAppId/SteamGameId env of every /proc/<pid>/environ. Logs pid-level
    add/end lines plus a compact summary when the active game set changes."""
    global STEAM_APP_SUMMARY, STEAM_PROC_SET
    procs = {}                 # pid -> (appid, name, cmd)
    games = {}                 # appid -> [pids]
    try:
        entries = os.listdir("/proc")
    except Exception:
        return
    for p in entries:
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/environ", "rb") as f:
                data = f.read()
        except Exception:
            continue
        appid = None
        for kv in data.split(b"\x00"):
            k, _, v = kv.partition(b"=")
            if k in (b"SteamAppId", b"SteamGameId"):
                appid = v.decode("ascii", "replace").strip() or None
                break
        if not appid:
            continue
        name = ""
        cmd = ""
        try:
            with open(f"/proc/{p}/comm") as f:
                name = f.read().strip()
        except Exception:
            pass
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(
                    "utf-8", "replace").strip()
        except Exception:
            pass
        procs[p] = (appid, name, cmd[:160])
        games.setdefault(appid, []).append(p)
    # -- pid-level transitions -------------------------------------------------
    old = STEAM_PROC_SET
    for p in sorted(set(procs) - set(old)):
        appid, name, cmd = procs[p]
        log(f"STEAM PROC: AppID {appid} running pid={p} name={name} "
            f"cmd={cmd or '(no cmdline)'}")
    for p in sorted(set(old) - set(procs)):
        appid, name, _cmd = old[p]
        log(f"STEAM PROC: AppID {appid} process ended (pid={p} name={name})")
    for p in sorted(set(old) & set(procs)):
        if procs[p][0] != old[p][0]:
            log(f"STEAM PROC: pid={p} AppID {old[p][0]} -> {procs[p][0]}")
    STEAM_PROC_SET = procs
    # -- compact summary when the active set changes ---------------------------
    summary = ", ".join(f"{a}[{len(ps)}]" for a, ps in sorted(games.items()))
    if not summary:
        summary = "(none)"
    if summary != STEAM_APP_SUMMARY:
        STEAM_APP_SUMMARY = summary
        log("STEAM PROC: running Steam games: " + summary)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    global LOGFILE

    ap = argparse.ArgumentParser(
        description="Passive diagnostic logger for the Legion Go 2 gyro patch "
                    "(InputPlumber -> virtual Steam Deck controller).")
    ap.add_argument("--log", default="/var/log/ip-gyro-logger.log",
                    help="log file path (default: %(default)s); output is also "
                         "mirrored to stdout for journalctl")
    args = ap.parse_args()

    # Open the log file BEFORE anything is logged; failure degrades to stdout-only.
    try:
        LOGFILE = open(args.log, "a", buffering=1)
    except Exception as e:
        LOGFILE = None
        print(f"WARNING: cannot open log file {args.log}: {e} "
              f"(continuing with stdout only)", flush=True)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log(f"LOGGER: {PROG} starting (pid={os.getpid()} cmd={' '.join(sys.argv)})")
    _log_startup_info()

    # udev monitor in a daemon thread (auto-restarts if udevadm dies).
    threading.Thread(target=udev_monitor_loop, daemon=True).start()

    devices = parse_proc_input_devices()
    set_id = device_set_id(devices)
    log_snapshot(devices, full=True)
    scan_evdev(devices, build_node_map(devices))
    discover_iio()

    # v2: open hidraw sources, snapshot Steam state, pull an InputPlumber
    # journal backlog, then start the journal follower thread.
    found_hid = scan_hidraw()
    state_tracker(devices, found_hid)
    steam_scan(time.monotonic())
    ipj_backlog()
    threading.Thread(target=ipj_tail_loop, daemon=True).start()
    log("LOGGER: v2 channels active — hidraw(17EF:61EB + 28DE:12F0/12FB/1205), "
        "InputPlumber journal, Steam registry + controller log")
    log("LOGGER: v3.1 decode active — 64-byte deck reports decoded per report "
        "(DECODE/MOTION), cross-direction ACTIVITY + FLOW GAP loss markers")
    log("LOGGER: v3.1 Steam Input activation tracking — controller_ui focus "
        "(STEAM UI / STEAM UI FOCUS) + running-game AppID (STEAM PROC) — "
        "direction D (Steam -> game)")
    log("LOGGER: v3.2 active — physical Legion-SRC IMU decode (IMU-LEGION + full "
        "raw hex), deck raw24-35 correlation, coalesced DECK bursts, fast IIO "
        "sampling during motion")
    log("LOGGER: v3.3 active — EVERY field of the Legion XInput frame decoded "
        "(DECODE ... XFULL: enums/batteries/sticks/buttons/triggers/touch/"
        "lq-gyro/IMU-timestamps/both accel+gyro/trailer) — full line on any "
        "change + decoded keepalive every 5 s with the raw-hex snapshot")

    last_snap = last_hb = last_iio = last_sess = last_iio_rescan = \
        last_flow = last_steam = time.monotonic()

    try:
        while not STOP:
            now = time.monotonic()

            # -- periodic: input device snapshot + evdev re-scan ------------
            if now - last_snap >= SNAP_INTERVAL:
                last_snap = now
                try:
                    devices = parse_proc_input_devices()
                    new_id = device_set_id(devices)
                    if new_id != set_id:
                        set_id = new_id
                        log_snapshot(devices, full=True)
                    scan_evdev(devices, build_node_map(devices))
                    found_hid = scan_hidraw()
                    state_tracker(devices, found_hid)
                except Exception as e:
                    log(f"LOGGER: snapshot/scan error: {e}")

            # -- periodic: compact heartbeat when nothing changed ------------
            if now - last_hb >= HB_INTERVAL:
                last_hb = now
                try:
                    log_snapshot(parse_proc_input_devices(), full=False)
                except Exception as e:
                    log(f"LOGGER: heartbeat error: {e}")

            # -- periodic: iio gyro/accel sampling ---------------------------
            # v3.2: adaptive cadence — fast (0.1 s) while a motion blip keeps
            # IIO_FAST_UNTIL armed, quiet 1/s otherwise (default preserved).
            iio_iv = IIO_FAST_INTERVAL if now < IIO_FAST_UNTIL else IIO_INTERVAL
            if now - last_iio >= iio_iv:
                last_iio = now
                try:
                    sample_iio(now)
                except Exception as e:
                    log(f"LOGGER: iio sampling error: {e}")

            # -- periodic: rediscover iio (IMU can come/go with mode switches) --
            if now - last_iio_rescan >= IIO_RESCAN_INTERVAL:
                last_iio_rescan = now
                try:
                    discover_iio()
                except Exception as e:
                    log(f"LOGGER: iio rediscover error: {e}")

            # -- periodic: session detection ----------------------------------
            if now - last_sess >= SESS_INTERVAL:
                last_sess = now
                try:
                    session_scan()
                except Exception as e:
                    log(f"LOGGER: session scan error: {e}")

            # -- periodic: per-second hidraw liveness + FLOW STOP/RESUME -----
            if now - last_flow >= HID_FLOW_INTERVAL:
                last_flow = now
                try:
                    emit_hid_summary(now)
                except Exception as e:
                    log(f"LOGGER: hid flow summary error: {e}")

            # -- periodic: Steam virtual-gamepad registry + controller log ----
            if now - last_steam >= STEAM_SCAN_INTERVAL:
                last_steam = now
                try:
                    steam_scan(now)
                except Exception as e:
                    log(f"LOGGER: steam scan error: {e}")

            # -- evdev + hidraw event capture (select drives loop cadence) ----
            try:
                handle_evdev(time.monotonic())
            except Exception as e:
                log(f"LOGGER: evdev loop error: {e}")
            try:
                handle_hidraw(time.monotonic())
            except Exception as e:
                log(f"LOGGER: hidraw loop error: {e}")
    finally:
        for node, entry in HID_DEVICES.items():
            try:
                os.close(entry["fd"])
            except Exception:
                pass
        HID_DEVICES.clear()
        for path, entry in EV_DEVICES.items():
            try:
                os.close(entry["fd"])
            except Exception:
                pass
        EV_DEVICES.clear()
        if LOGFILE is not None:
            try:
                LOGFILE.flush()
                LOGFILE.close()
            except Exception:
                pass
        log("LOGGER: shutdown complete")


if __name__ == "__main__":
    main()
