# -*- coding: utf-8 -*-
"""
ipgyro.mouseobs — v3.16 (todo 53e) MOUSE-OUTPUT OBSERVER (Part 2).

WHY THIS MODULE EXISTS (research 53d)
-------------------------------------
When the ACTIVE Steam Input config binds gyro to MOUSE (e.g. the desktop/shell
config 413080 after wake), the deck's gyro is injected as a SEPARATE pointer
motion — NOT onto the Steam pad (28de:11ff) as ABS_RX/ABS_RY. The v3.15 logger
only watched the pad, so a gyro->mouse config looked like "gyro died" (0 pad
gyro-axis) when in fact gyro was being delivered as pointer motion on the
desktop. This observer counts REL_X/REL_Y pointer motion so the binding-aware
gyro verdict can prove [GYRO→MOUSE] (pointer motion delivered while the config
binds gyro to mouse + deck motion present) vs [GYRO-STALL] (deck motion present
but no pointer motion delivered).

AUTO-DETECTION (environment-dependent, never assumed)
-----------------------------------------------------
The logger cannot know a priori whether gyro->mouse lands on a SEPARATE virtual
mouse evdev node (Steam/InputPlumber uinput pointer) or is injected through the
desktop compositor (extest / libei on Wayland/KDE), which may NOT surface as a
distinct evdev node the logger can attribute to the deck. So this module:
  * enumerates every evdev node that exposes REL_X/REL_Y (mouse-capable);
  * auto-detects a "virtual pointer" node: a REL-capable node whose name hints
    Steam/InputPlumber/virtual (28de-ish / "Steam" / "InputPlumber" / "virtual")
    vs the physical hardware mouse;
  * counts REL_X/REL_Y motion on the detected pointer node(s) per second.
When no distinct virtual-mouse node is found, MOUSE_MODE='extest' and the
observer counts REL motion on the desktop pointer nodes as best-effort evidence
that pointer motion IS being delivered (the logger states the attribution
honestly — it proves pointer motion is present on the desktop while the config
binds gyro to mouse + deck motion, not that the game's camera reacted).

Shared mutable state is accessed ONLY as S.<name>.
"""

import os
import re
import select
import struct
import time

from . import state as S
from .conf import EVENT_SIZE
from .logcore import log

EV_REL = 0x02
REL_X = 0x00
REL_Y = 0x01

# Name hints for a VIRTUAL pointer (Steam/InputPlumber uinput mouse) vs the
# physical hardware mouse. A REL-capable node matching these is the gyro->mouse
# output candidate; the physical mouse is excluded from the "virtual" verdict.
_VIRTUAL_MOUSE_HINTS = (
    "steam", "inputplumber", "virtual", "uinput", "28de", "11ff",
    "x-box", "xbox", "controller", "gamepad", "deck",
)
_PHYSICAL_MOUSE_HINTS = (
    "mouse", "trackpoint", "trackpad", "touchpad", "synaptics", "elantech",
    "logitech", "razer", "dell", "msi", "lenovo", "wacom",
)

# /proc/bus/input/devices 'I:' line carries Bus/Vendor/Product.
_I_LINE_RE = re.compile(
    r"Bus=([0-9A-Fa-f]+)\s+Vendor=([0-9A-Fa-f]+)\s+Product=([0-9A-Fa-f]+)")


def _bitmap_has(bmap, field, bit):
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


def _parse_proc_input_devices():
    """Parse /proc/bus/input/devices into device dicts (name, handlers, bmap,
    vid, pid)."""
    devices = []
    try:
        with open("/proc/bus/input/devices") as f:
            lines = f.read().splitlines()
    except Exception:
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
                cur = {"name": "", "handlers": [], "bmap": {}, "vid": "",
                       "pid": ""}
            if line[0] == "I":
                m = _I_LINE_RE.search(val)
                if m:
                    cur["vid"] = m.group(2).upper()
                    cur["pid"] = m.group(3).upper()
            elif line[0] == "N":
                cur["name"] = val.partition("=")[2].strip().strip('"')
            elif line[0] == "H":
                cur["handlers"] = val.partition("=")[2].split()
            elif line[0] == "B":
                field, _, rest = val.partition("=")
                cur["bmap"][field] = rest
    if cur:
        devices.append(cur)
    return devices


def _rel_capable(dev):
    """True when a device exposes REL_X or REL_Y (mouse-like)."""
    b = dev.get("bmap") or {}
    has_rel = _bitmap_has(b, "EV", EV_REL)
    if not has_rel:
        return False
    return (_bitmap_has(b, "REL", REL_X) or _bitmap_has(b, "REL", REL_Y))


def _is_virtual_pointer(dev):
    """True when a REL-capable node looks like a VIRTUAL pointer (Steam/
    InputPlumber/uinput/controller) rather than the physical hardware mouse."""
    name = (dev.get("name") or "").lower()
    if any(h in name for h in _PHYSICAL_MOUSE_HINTS) and \
            not any(h in name for h in _VIRTUAL_MOUSE_HINTS):
        return False
    return any(h in name for h in _VIRTUAL_MOUSE_HINTS)


def _event_node(dev):
    for h in dev.get("handlers") or []:
        if h.startswith("event"):
            return "/dev/input/" + h
    return None


def detect_mouse_nodes():
    """Auto-detect the mouse-output nodes. Returns (mode, node_list):
      mode = 'node'  when a distinct virtual-pointer evdev node was found
             'extest' when no virtual node but REL-capable desktop pointers exist
             'none'  when no REL-capable node is present
      node_list = the '/dev/input/eventN' paths to count REL motion on."""
    devices = _parse_proc_input_devices()
    rel_nodes = []
    virtual_nodes = []
    for d in devices:
        if not _rel_capable(d):
            continue
        node = _event_node(d)
        if node is None:
            continue
        rel_nodes.append((node, d.get("name") or "?"))
        if _is_virtual_pointer(d):
            virtual_nodes.append((node, d.get("name") or "?"))
    if virtual_nodes:
        return "node", virtual_nodes
    if rel_nodes:
        return "extest", rel_nodes
    return "none", []


# ---------------------------------------------------------------------------
# Lightweight REL reader (raw evdev, mirrors devices.py's approach)
# ---------------------------------------------------------------------------

class _MouseReader:
    """Opens the detected mouse node(s) and counts REL_X/REL_Y per second into
    S.MOUSE_REL_COUNT. Runs in a daemon thread independent of the cadence loop
    (so a main-loop stall never hides pointer motion). Re-scans the node set on
    churn. Never blocks the process (non-blocking reads)."""

    def __init__(self):
        self._fds = {}          # path -> fd
        self._bufs = {}         # path -> bytearray
        self._mode = "none"
        self._nodes = []

    def _rescan(self):
        mode, nodes = detect_mouse_nodes()
        self._mode = mode
        self._nodes = nodes
        # close fds no longer wanted
        wanted = {p for p, _n in nodes}
        for p in list(self._fds):
            if p not in wanted:
                try:
                    os.close(self._fds[p])
                except Exception:
                    pass
                self._fds.pop(p, None)
                self._bufs.pop(p, None)
        # open new fds
        for p, _n in nodes:
            if p in self._fds:
                continue
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self._fds[p] = fd
            self._bufs[p] = bytearray()

    def run(self):
        S.MOUSE_MODE = "none"
        last_rescan = 0.0
        while not S.STOP:
            now = time.monotonic()
            if now - last_rescan >= 3.0:
                last_rescan = now
                try:
                    self._rescan()
                except Exception:
                    pass
                if self._mode != S.MOUSE_MODE:
                    S.MOUSE_MODE = self._mode
                    if self._mode == "node":
                        log(f"[MOUSE-OBS] virtual pointer node(s) auto-detected: "
                            f"{', '.join(n for _p, n in self._nodes) or '-'} "
                            f"({', '.join(p for p, _n in self._nodes) or '-'}) — "
                            f"counting REL motion as gyro->mouse evidence")
                    elif self._mode == "extest":
                        log("[MOUSE-OBS] no distinct virtual-mouse node; pointer "
                            "motion is extest/compositor-injected on the desktop "
                            "(REL-capable nodes present) — counting desktop REL "
                            "motion as best-effort gyro->mouse evidence")
                    else:
                        log("[MOUSE-OBS] no REL-capable pointer node present — "
                            "gyro->mouse output not observable host-side")
            if not self._fds:
                time.sleep(0.5)
                continue
            try:
                ready, _, _ = select.select(list(self._fds.values()), [], [], 0.05)
            except InterruptedError:
                continue
            for p, fd in list(self._fds.items()):
                if fd not in ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    self._fds.pop(p, None)
                    self._bufs.pop(p, None)
                    continue
                if not chunk:
                    continue
                self._bufs[p].extend(chunk)
                buf = self._bufs[p]
                while len(buf) >= EVENT_SIZE:
                    raw = bytes(buf[:EVENT_SIZE])
                    del buf[:EVENT_SIZE]
                    try:
                        _sec, _usec, etype, code, value = struct.unpack(
                            "<2qHHi", raw)
                    except struct.error:
                        buf.clear()
                        break
                    if etype == EV_REL and code in (REL_X, REL_Y):
                        S.MOUSE_REL_COUNT += 1
                        S.MOUSE_REL_LAST = now


_reader = None
_reader_lock = None


def start_mouse_observer():
    """Start the mouse-output observer thread once. Idempotent. Never raises."""
    global _reader, _reader_lock
    if _reader is not None:
        return
    try:
        import threading
        _reader_lock = threading.Lock()
        _reader = _MouseReader()
        threading.Thread(target=_reader.run, daemon=True).start()
        log("MOUSE-OBS: mouse-output observer thread started (REL_X/REL_Y "
            "pointer motion counter for gyro->mouse evidence)")
    except Exception as e:
        log(f"MOUSE-OBS: observer thread start error: {e}")
        _reader = None
