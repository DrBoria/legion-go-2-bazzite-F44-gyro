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

# v3.11 (todo 52o) — the GAME-FACING virtual pad Steam creates. Steam Input
# reads the deck (28de:12f0) and emits a SEPARATE uinput evdev node named
# "Microsoft X-Box 360 pad N" with VID:PID 28de:11ff that the GAME (ESO via
# Proton/XInput/SDL/evdev) actually reads. This is the ONLY host-side proof of
# game delivery. Identity is matched by NAME PREFIX + VID:PID — NEVER the
# churning eventN (event numbers change on every Steam teardown/recreate).
PAD_NAME_PREFIX = "Microsoft X-Box 360 pad"
PAD_VID = "28DE"
PAD_PID = "11FF"
# Gyro axes on the pad (Steam maps gyro to ABS_RX/ABS_RY when the active config
# binds gyro to the right-stick axes). Only these make gyro PROVABLE through
# the pad; if gyro is bound to mouse it emits on a different (virtual mouse)
# node and is NOT provable host-side through the pad.
PAD_GYRO_AXES = (0x03, 0x04)   # ABS_RX, ABS_RY
# The /proc/bus/input/devices 'I:' line carries the authoritative bus/vendor/
# product (e.g. 'I: Bus=0003 Vendor=28de Product=11ff Version=0001').
_I_LINE_RE = re.compile(
    r"Bus=([0-9A-Fa-f]+)\s+Vendor=([0-9A-Fa-f]+)\s+Product=([0-9A-Fa-f]+)")


# ---------------------------------------------------------------------------
# /proc/bus/input/devices parsing
# ---------------------------------------------------------------------------

def parse_proc_input_devices():
    """Return a list of device dicts parsed from /proc/bus/input/devices.
    v3.11: each entry also carries the authoritative bus/vendor/product parsed
    from the 'I:' line (e.g. Bus=0003 Vendor=28de Product=11ff) so a device can
    be matched by VID:PID without depending on the sysfs path format."""
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
                cur = {"name": "", "phys": "", "sysfs": "", "handlers": [],
                       "bmap": {}, "bus": "", "vid": "", "pid": ""}
            # /proc/bus/input/devices lines carry the field name inside the value,
            # e.g. 'N: Name="Power Button"', 'P: Phys=...', 'H: Handlers=kbd event0'.
            if line[0] == "I":
                m = _I_LINE_RE.search(val)
                if m:
                    cur["bus"] = m.group(1).upper()
                    cur["vid"] = m.group(2).upper()
                    cur["pid"] = m.group(3).upper()
            elif line[0] == "N":
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


# ---------------------------------------------------------------------------
# v3.11 (todo 52o) — the game-facing virtual pad (28de:11ff) identity.
# ---------------------------------------------------------------------------

def _is_pad(dev):
    """True when a /proc/bus/input/devices entry is Steam's game-facing virtual
    pad: name starts with 'Microsoft X-Box 360 pad' AND VID:PID 28de:11ff (from
    the 'I:' line). Matched by NAME + VID:PID, NEVER by the churning eventN."""
    name = (dev.get("name") or "").strip()
    if not name.startswith(PAD_NAME_PREFIX):
        return False
    return (dev.get("vid") == PAD_VID and dev.get("pid") == PAD_PID)


def _pad_event_node(dev):
    """The '/dev/input/eventN' node of a pad entry (first event handler), or
    None. The eventN number churns across Steam teardown/recreate — callers
    must always re-derive it from the current device set, never cache it."""
    for h in dev.get("handlers") or []:
        if h.startswith("event"):
            return "/dev/input/" + h
    return None


def _scan_pad_identity(devices):
    """(Re)derive the current game-facing pad identity from the device set and
    publish it on S. Detects [PAD-RECREATED] when the pad's eventN node changed
    (Steam tore the pad down and recreated it — Valve steam-for-linux#13029
    mode) and [GAME-LOST-PAD] when the pad node disappeared entirely while a
    game reader was present. Returns the pad entry or None."""
    pad = next((d for d in devices if _is_pad(d)), None)
    if pad is None:
        # pad node gone entirely -> [GAME-LOST-PAD] if a game reader was known
        if S.PAD_NODE is not None and S.PAD_READER_KNOWN:
            now = time.monotonic()
            if now - S.PAD_GAME_LOST_LAST >= 5.0:
                S.PAD_GAME_LOST_LAST = now
                log(f"[GAME-LOST-PAD] pad node {S.PAD_NODE} GONE while a game "
                    f"reader held it (Steam tore the virtual pad down; game "
                    f"lost the pad fd — Valve steam-for-linux#13029)")
        S.PAD_NODE = None
        S.PAD_SYS = None
        S.PAD_NAME = None
        S.PAD_READER_KNOWN = None
        return None
    node = _pad_event_node(pad)
    name = (pad.get("name") or "").strip()
    sysfs = pad.get("sysfs") or ""
    if S.PAD_NODE is not None and node is not None and node != S.PAD_NODE:
        now = time.monotonic()
        if now - S.PAD_RECREATED_LAST >= 5.0:
            S.PAD_RECREATED_LAST = now
            log(f"[PAD-RECREATED] pad eventN changed {S.PAD_NODE} -> {node} "
                f"(Steam recreated the virtual pad {name}; old node torn down)")
    S.PAD_NODE = node
    S.PAD_SYS = sysfs
    S.PAD_NAME = name
    return pad


def scan_evdev(devices, node_map):
    """Open evdev nodes for every gamepad-like device; close nodes that are
    gone. Called every snapshot cycle so devices come/go cleanly. v3.11: also
    re-derives the game-facing virtual pad (28de:11ff) identity and tags its
    EV_DEVICES entry so pad EV events are counted as [PAD→GAME] proof."""
    # v3.11 — derive the pad identity FIRST (before the open loop) so the pad
    # node is always captured even if it is not gamepad-heuristic-matched.
    pad = _scan_pad_identity(devices)
    pad_node = _pad_event_node(pad) if pad is not None else None

    desired = {}
    evr = S.EVREADER_ON
    for d in devices:
        # v3.11 — the game-facing pad must ALWAYS be captured even if the
        # gamepad heuristic ever fails to match it (it is the delivery proof).
        if not (is_gamepad(d) or _is_pad(d)):
            continue
        # v3.12 (todo 52r PART 2) — when the python-evdev reader owns the pad
        # (S.EVREADER_ON), the pad node is EXCLUDED from this raw capture:
        # evdev is multicast, so TWO open fds on the same node each see EVERY
        # event; counting them twice (raw _process_event + evreader drain_events
        # into the SAME pad counters) would DOUBLE pad_ev/s / pad_key/s /
        # pad_abs/s / pad_gyro/s. When EVREADER_ON drops False (evdev reader
        # stopped/unavailable), this reopens the pad via the raw fallback.
        if evr and _is_pad(d):
            continue
        for h in d.get("handlers") or []:
            if h.startswith("event"):
                path = "/dev/input/" + h
                desired[path] = node_map.get(h) or d.get("name") or ""

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
        is_pad = (path == pad_node)
        S.EV_DEVICES[path] = {
            "fd": fd, "name": nm, "buf": bytearray(), "last_abs": 0.0,
            "is_pad": is_pad,
        }
        if is_pad:
            log(f"EV: capturing {path} ({nm or '?'}) — GAME-FACING VIRTUAL PAD "
                f"(28de:11ff); EV events here + a game pid holding the fd = "
                f"proof input reached the game")
        else:
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
    """Log EV_KEY presses (value 1) and EV_ABS changes (throttled). v3.11: when
    the entry is the game-facing virtual pad (28de:11ff), every EV event is
    counted as [PAD→GAME] proof evidence (pad_ev/s), and EV_ABS on the gyro
    axes (ABS_RX/ABS_RY) is counted separately so gyro provability through the
    pad can be asserted ([GYRO→PAD-AXIS]).
    v3.12 (todo 52r FIX-C): BUTTONS and GYRO are counted as INDEPENDENT
    per-data-type streams with SEPARATE counters — pad_key/s (EV_KEY buttons),
    pad_abs/s (non-gyro EV_ABS sticks/triggers) and pad_gyro/s (ABS_RX/ABS_RY)
    — so one busy stream can never starve the other's verdict (the v3.11
    shared-latch starvation bug)."""
    is_pad = bool(entry.get("is_pad"))
    # v3.12 (todo 52r PART 2) — belt-and-suspenders with the scan_evdev
    # exclusion: when the python-evdev reader owns the pad (S.EVREADER_ON),
    # skip raw processing of pad events entirely. This covers the transient
    # window where the raw pad fd is still open before the next scan_evdev
    # closes it — evdev multicast would otherwise double-count every pad event.
    if is_pad and S.EVREADER_ON:
        return
    if etype == EV_KEY:
        if value == 1:
            kname = KEY_NAMES.get(code, f"0x{code:03x}")
            log(f"EV {name} KEY {kname} DOWN")
        S.EV_ACT_COUNT += 1
        S.EV_ACT_LAST = now
        if is_pad:
            S.PAD_EV_COUNT += 1
            S.PAD_KEY_COUNT += 1
            S.PAD_KEY_LAST = now
            S.PAD_LAST_EV = now
    elif etype == EV_ABS:
        S.EV_ACT_COUNT += 1
        S.EV_ACT_LAST = now
        if is_pad:
            S.PAD_EV_COUNT += 1
            S.PAD_ABS_COUNT += 1
            S.PAD_LAST_EV = now
            if code in PAD_GYRO_AXES:
                S.PAD_GYRO_AXIS_COUNT += 1
                S.PAD_GYRO_LAST = now
            else:
                # non-gyro axis (stick/trigger) — its OWN counter (FIX-C)
                S.PAD_STICK_COUNT += 1
                S.PAD_STICK_LAST = now
        if now - entry["last_abs"] >= EV_THROTTLE:
            aname = ABS_NAMES.get(code, f"0x{code:02x}")
            log(f"EV {name} ABS {aname} {value}")
            entry["last_abs"] = now
    # EV_SYN (0x00) and everything else are intentionally ignored.
