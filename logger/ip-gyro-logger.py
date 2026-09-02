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

    last_snap = last_hb = last_iio = last_sess = last_iio_rescan = time.monotonic()

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

            # -- evdev event capture (select drives the loop cadence) ---------
            try:
                handle_evdev(time.monotonic())
            except Exception as e:
                log(f"LOGGER: evdev loop error: {e}")
    finally:
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
