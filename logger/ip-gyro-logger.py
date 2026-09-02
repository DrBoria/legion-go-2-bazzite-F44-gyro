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

# v2 — hidraw / Steam / InputPlumber journal state
HID_DEVICES = {}            # hidraw node -> {fd,path,label,vid,pid,iface,...}
HID_OPEN_ERR = {}           # /dev/hidrawN -> last open-error time (anti-spam)
STEAM_CACHE = {}            # steam file path -> {reg_sig / ctl_pos / ...}
IPJ_FLOOD = [0.0, 0]        # InputPlumber journal flood gate [window, lines]
LAST_STATE = None           # last computed mode/attach signature (for diffs)

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
    magnitude is non-zero (max ~1 line/device/sec), idle heartbeat every 5s."""
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
            if now - IIO_LAST_ACTIVE.get(entry, 0.0) >= IIO_INTERVAL:
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
    if etype == EV_KEY:
        if value == 1:
            kname = KEY_NAMES.get(code, f"0x{code:03x}")
            log(f"EV {name} KEY {kname} DOWN")
    elif etype == EV_ABS:
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

def _decode_deck(raw):
    """Best-effort decode of a virtual-deck report. Shipped layout puts the
    gyro at pitch=bytes 30-31, yaw=32-33, roll=34-35 (signed 16-bit LE)."""
    if len(raw) < 36:
        return {}
    return {"pitch": _s16(raw, 30), "yaw": _s16(raw, 32), "roll": _s16(raw, 34)}

def _maybe_decode_deck(chunk):
    L = len(chunk)
    if L == 0:
        return {}
    # single report with a gyro tail, or a tail of back-to-back 36-byte reports
    if 36 <= L <= 40:
        return _decode_deck(chunk)
    if L > 40 and L % 36 == 0:
        return _decode_deck(chunk[-36:])
    return {}

def handle_hidraw(now):
    """select() on all open hidraw fds; count reports, keep a decode sample,
    and note first-seen frame lengths (wrong/odd formats become visible)."""
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
        # first time we see a given frame length, log it (format signature)
        if (len(chunk) <= 128 and len(chunk) not in e["frames_seen"]
                and len(e["frames_seen"]) < 32):
            e["frames_seen"][len(chunk)] = t2
            log(f"HID {_hid_label(e)} FRAME len={len(chunk)} "
                f"head={chunk[:12].hex(' ')}")
        if t2 - e["last_raw"] >= HID_RAW_SAMPLE:
            e["last_raw"] = t2
            e["last_hex"] = chunk[:24].hex(" ")
        if e["pid"] in DECK_PIDS:
            dec = _maybe_decode_deck(chunk)
            if dec:
                e["last_dec"] = dec
        e["last_data"] = t2

def emit_hid_summary(now):
    """Once per second: one liveness line per watched hidraw, plus FLOW STOP
    when a source goes quiet while still present (connection lost HERE)."""
    for node, e in list(HID_DEVICES.items()):
        label = _hid_label(e)
        reads = e["count"]
        nbytes = e["nbytes"]
        e["count"] = 0
        e["nbytes"] = 0
        if reads:
            dec = e.get("last_dec") or {}
            dstr = ""
            if dec:
                dstr = (" gyro(p,y,r)={},{},{}".format(
                    _fmt(dec.get("pitch")), _fmt(dec.get("yaw")),
                    _fmt(dec.get("roll"))))
            log(f"HIDFLOW {label} {reads} rd/s {nbytes} B/s "
                f"len={e['last_len']}{dstr}")
            e["stopped"] = False
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
            continue
        silent = now - last
        if silent >= FLOW_STOP_AFTER and not e["stopped"]:
            e["stopped"] = True
            e["stopped_at"] = now
            log(f"FLOW STOP {label} (no data for {silent:.1f}s, "
                f"device still present)")
        elif e["stopped"] and now - e["stopped_at"] >= FLOW_STOP_HB:
            e["stopped_at"] = now
            log(f"FLOW STOP {label} (still silent, "
                f"{now - last:.0f}s since last data)")

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
    """Diff the Steam registry + controller log for every desktop user."""
    bases = _steam_bases()
    if not bases:
        return
    for base in bases:
        _steam_registry(os.path.join(
            base, ".local/share/Steam/config/virtualgamepadinfo.txt"))
        _steam_controller_log(os.path.join(
            base, ".local/share/Steam/logs/controller.txt"))

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
            if now - last_iio >= IIO_INTERVAL:
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
