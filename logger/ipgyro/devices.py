# -*- coding: utf-8 -*-
"""
ipgyro.devices — /proc/bus/input/devices parsing, gamepad heuristics,
device-set identity, snapshots/heartbeats, the evdev event capture path
(open/scan/drop/read/decode) and loginctl+gamescope session detection.

Shared mutable state (EV_DEVICES/EV_OPEN_ERR/SESSION_SET/GAMESCOPE_PID/
EV_ACT_COUNT/EV_ACT_LAST) is ALWAYS accessed as S.<name> (see ipgyro.state);
constants are imported by value from ipgyro.conf (never rebound).
"""

import hashlib
import os
import re
import select
import struct
import time

from . import state as S
from .conf import (
    ABS_NAMES,
    EV_ABS,
    EV_KEY,
    EV_OPEN_ERR_HB,
    EV_THROTTLE,
    EVENT_SIZE,
    GAMEPAD_ABS_BITS,
    GAMEPAD_KEY_BITS,
    KEY_NAMES,
    NAME_GAMEPAD_HINTS,
)
from .logcore import log, run_cmd

# v3.6 (R4) — VID:PID embedded in a sysfs path segment like
# ".../0003:17EF:61EB.0001/input/input6/event3" (also covers 28DE deck nodes).
_VIDPID_RE = re.compile(r"(?:0003|0005):([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})")


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


def _device_identity(dev):
    """Derive full identity metadata for one /proc/bus/input/devices entry:
    VID:PID (from the sysfs path), serial (uniq), class (gamepad/other), plus
    the raw name/phys/sysfs/handlers. Returns a dict of human strings."""
    sysfs = dev.get("sysfs") or ""
    vid = pid = ""
    m = _VIDPID_RE.search(sysfs)
    if m:
        vid, pid = m.group(1).upper(), m.group(2).upper()
    cls = "gamepad" if is_gamepad(dev) else "other"
    return {
        "name": dev.get("name") or "?",
        "vid": vid, "pid": pid,
        "serial": dev.get("uniq") or "",
        "phys": dev.get("phys") or "",
        "sysfs": sysfs,
        "handlers": " ".join(dev.get("handlers") or []),
        "class": cls,
    }


def log_device_inventory(devices, reason):
    """(R4) One loud [DEV-INV] inventory line PER device with FULL identity
    metadata (VID:PID, serial, syspath, evdev/hidraw node, class, name), for
    every connected device. Emitted at startup (reason=startup) and on each
    attach/detach (reason=attach/detach) so a full context snapshot exists,
    not just a delta line. Change-only re-assert via S.DEV_INV_SIG."""
    try:
        inv = []
        for d in sorted(devices, key=lambda x: (x.get("sysfs") or "",
                                                x.get("name") or "")):
            ident = _device_identity(d)
            inv.append(
                f"[DEV-INV] reason={reason} class={ident['class']} "
                f"name={ident['name']} VID:PID={ident['vid']}:{ident['pid'] or '?'} "
                f"serial={ident['serial'] or '-'} phys={ident['phys'] or '-'} "
                f"syspath={ident['sysfs'] or '-'} nodes={ident['handlers'] or '-'}")
        sig = hashlib.md5("\n".join(inv).encode("utf-8", "replace")).hexdigest()
        if reason == "startup" or sig != S.DEV_INV_SIG:
            S.DEV_INV_SIG = sig
            log(f"=== DEVICE INVENTORY (reason={reason}, {len(inv)} devices) ===")
            for line in inv:
                log(line)
    except Exception as e:
        log(f"LOGGER: device inventory error: {e}")


def log_snapshot(devices, full=True):
    """Log a full snapshot (device set changed) or a compact heartbeat line."""
    try:
        if full:
            log("=== SNAPSHOT (device set changed) ===")
            for d in sorted(devices, key=lambda x: (x.get("sysfs") or "", x.get("name") or "")):
                tag = "*" if is_gamepad(d) else " "
                ident = _device_identity(d)
                log("  [{}] name={} VID:PID={}:{} serial={} phys={} "
                    "handlers={} sysfs={}".format(
                        tag, ident["name"], ident["vid"],
                        ident["pid"] or "?", ident["serial"] or "-",
                        ident["phys"], ident["handlers"], ident["sysfs"]))
        gp = sum(1 for d in devices if is_gamepad(d))
        ev = sum(1 for d in devices for h in (d.get("handlers") or []) if h.startswith("event"))
        log(f"HB: devices={len(devices)} gamepads={gp} event_nodes={ev}")
    except Exception as e:
        log(f"LOGGER: snapshot error: {e}")


# ---------------------------------------------------------------------------
# Session detection (loginctl + gamescope)
# ---------------------------------------------------------------------------

def session_scan():
    """Log session start/stop transitions from loginctl, plus gamescope
    presence as the 'gaming mode' signal."""
    out = run_cmd(["loginctl", "list-sessions", "--no-legend"]) or ""
    cur = {}
    for line in out.splitlines():
        parts = line.split()
        if parts:
            cur[parts[0]] = line.strip()
    for sid, line in cur.items():
        if sid not in S.SESSION_SET:
            log(f"SESSION: started {line}")
    for sid in list(S.SESSION_SET.keys()):
        if sid not in cur:
            log(f"SESSION: ended {S.SESSION_SET[sid]}")
    S.SESSION_SET = cur

    gp = run_cmd(["pgrep", "-x", "gamescope"])
    new_pid = gp or None
    if new_pid != S.GAMESCOPE_PID:
        if new_pid:
            log(f"SESSION: gamescope started (pid={new_pid})")
        else:
            log("SESSION: gamescope stopped")
        S.GAMESCOPE_PID = new_pid


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
        if path in S.EV_DEVICES:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            # Permission denied for non-root is expected; log it, don't spam.
            if now - S.EV_OPEN_ERR.get(path, 0.0) >= EV_OPEN_ERR_HB:
                log(f"EV: cannot open {path}: {e}")
                S.EV_OPEN_ERR[path] = now
            continue
        S.EV_DEVICES[path] = {"fd": fd, "name": nm, "buf": bytearray(), "last_abs": 0.0}
        log(f"EV: capturing {path} ({nm or '?'})")

    for path in list(S.EV_DEVICES.keys()):
        if path not in desired:
            _drop_ev(path)
            log(f"EV: closed {path} (device gone)")


def _drop_ev(path):
    try:
        os.close(S.EV_DEVICES[path]["fd"])
    except Exception:
        pass
    S.EV_DEVICES.pop(path, None)


def handle_evdev(now):
    """select() on all open evdev fds and parse any input_event structs."""
    if not S.EV_DEVICES:
        return
    fds = [e["fd"] for e in S.EV_DEVICES.values()]
    try:
        ready, _, _ = select.select(fds, [], [], 0.05)
    except InterruptedError:
        return

    for path, entry in list(S.EV_DEVICES.items()):
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
        S.EV_ACT_COUNT += 1
        S.EV_ACT_LAST = now
    elif etype == EV_ABS:
        S.EV_ACT_COUNT += 1
        S.EV_ACT_LAST = now
        if now - entry["last_abs"] >= EV_THROTTLE:
            aname = ABS_NAMES.get(code, f"0x{code:02x}")
            log(f"EV {name} ABS {aname} {value}")
            entry["last_abs"] = now
    # EV_SYN (0x00) and everything else are intentionally ignored.
