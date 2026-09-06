# -*- coding: utf-8 -*-
"""
ipgyro.evreader — python-evdev transport for the game-facing virtual pad
(todo 52r, LOG_VERSION v3.12, PART 2: adopt the 52q ready-made backbone).

WHY THIS MODULE EXISTS
----------------------
The v3.11 logger hand-rolled its evdev capture (raw os.open/select/struct on
/dev/input/eventN in devices.py). Research (todo 52q) concluded: stop
hand-rolling the transport; use python-evdev (package `evdev`) with a thin
custom wrapper. python-evdev gives us:
  * stable identity matching (name prefix "Microsoft X-Box 360 pad" + VID/PID
    via uevent where present — never the churning eventN);
  * multiple devices read in one process with one clock;
  * an in-process reader that writes to the log NON-BLOCKINGLY (bounded
    queue / drop-oldest under pressure — eliminates the class of the ~30 s
    blocking-pipe stall);
  * auto-reopen that follows pad recreation across detach/wake (watch
    /proc/bus/input/devices for the pad name; on eventN change log
    [PAD-RECREATED] old->new and continue seamlessly).

The physical XInput hop is HID (hidraw), NOT evdev — python-evdev does not do
hidraw, so that hop keeps the existing raw-hidraw capture (capture.py/codecs.py)
unchanged. This module ONLY owns the evdev-visible hops: the Steam pad
(28de:11ff) primarily, and the deck when it surfaces an evdev node.

python-evdev is OPTIONAL: if `import evdev` fails (package not installed in the
service interpreter), this module degrades gracefully to the existing raw
capture in devices.py (S.EVREADER_ON stays False) — the logger still works, it
just uses the hand-rolled reader. The service interpreter is chosen so evdev
imports (system python3 has evdev on this host; no venv needed).

Shared mutable state is ALWAYS accessed as S.<name> (see ipgyro.state).
"""

import os
import threading
import time

from . import state as S
from .logcore import log

# python-evdev availability (checked once at import; never raises).
try:
    import evdev as _evdev
    from evdev import InputDevice, ecodes as _ecodes  # noqa: F401
    EVDEV_AVAILABLE = True
except Exception:
    _evdev = None
    EVDEV_AVAILABLE = False

# The pad identity (name prefix + VID:PID) mirrors devices.py — kept here so
# evreader can self-identify without importing devices (cycle-safe).
PAD_NAME_PREFIX = "Microsoft X-Box 360 pad"
PAD_VID = "28DE"
PAD_PID = "11FF"
# Gyro axes on the pad (Steam maps gyro to ABS_RX/ABS_RY when the active config
# binds gyro to the right-stick axes).
PAD_GYRO_AXES = (0x03, 0x04)   # ABS_RX, ABS_RY

# Bounded non-blocking delivery queue: the reader thread pushes raw events; the
# log writer drains them. Under pressure the OLDEST events are dropped (never
# block the reader, never freeze the process — the v3.10 D1 lesson applied to
# the evdev path).
_MAX_QUEUE = 20000

_reader_lock = threading.Lock()
_queue = []          # list of (etype, code, value) tuples (FIFO via index)
_queue_dropped = 0   # count of dropped events under pressure


# ---------------------------------------------------------------------------
# python-evdev device discovery (stable identity, churn-following)
# ---------------------------------------------------------------------------

def _list_pad_nodes():
    """Return the current '/dev/input/eventN' of the 28de:11ff pad via
    python-evdev's list_devices + InputDevice identity (name prefix + VID/PID
    from uevent where present). Returns None when absent. Never raises."""
    if not EVDEV_AVAILABLE:
        return None
    try:
        paths = _evdev.list_devices()
    except Exception:
        return None
    for path in paths:
        try:
            dev = InputDevice(path)
        except Exception:
            continue
        try:
            name = (dev.name or "").strip()
            if not name.startswith(PAD_NAME_PREFIX):
                continue
            # VID/PID from uevent (present on uinput-backed Steam pads)
            vid = (dev.info.vendor or 0)
            pid = (dev.info.product or 0)
            if vid and pid:
                if (f"{vid:04X}" == PAD_VID and f"{pid:04X}" == PAD_PID):
                    return path
                continue
            # no VID/PID in uevent -> trust the name prefix (fallback)
            return path
        finally:
            try:
                dev.close()
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Reader thread body (independent of the cadence loop)
# ---------------------------------------------------------------------------

def _run_reader():
    """Daemon thread: follow the pad node with python-evdev, pushing raw events
    onto the bounded queue. Auto-reopens when the pad node churns (detach/wake/
    Steam teardown) — logs [PAD-RECREATED] old->new and continues seamlessly.
    Never blocks the process (bounded queue, non-blocking read loop)."""
    S.EVREADER_ON = True
    last_node = None
    while not S.STOP:
        node = _list_pad_nodes()
        if node is None:
            if last_node is not None:
                log(f"[PAD-RECREATED] pad node GONE ({last_node}) — Steam tore "
                    f"the virtual pad down; waiting for recreation")
                last_node = None
            time.sleep(1.0)
            continue
        if node != last_node:
            if last_node is not None:
                log(f"[PAD-RECREATED] pad eventN changed {last_node} -> {node} "
                    f"(Steam recreated the virtual pad; python-evdev re-opened)")
            else:
                log(f"EVDEV: python-evdev pad reader opened {node} "
                    f"({PAD_NAME_PREFIX}, {PAD_VID}:{PAD_PID})")
            last_node = node
            S.PAD_NODE = node
        try:
            dev = InputDevice(node)
        except Exception as e:
            S.EVREADER_ERR = str(e)
            time.sleep(1.0)
            continue
        S.EVREADER_ERR = ""
        # NOTE: we deliberately do NOT dev.grab() — the game (ESO via Proton)
        # must keep reading the same pad node. python-evdev read_loop() is a
        # SHARED reader (a second reader on the node receives the same stream),
        # so it never steals events from the game.
        try:
            for event in dev.read_loop():
                if S.STOP:
                    break
                _push(event.type, event.code, event.value)
        except Exception as e:
            S.EVREADER_ERR = str(e)
        finally:
            try:
                dev.close()
            except Exception:
                pass
        # node vanished / read error -> re-scan (churn-follow)
        time.sleep(0.5)
    S.EVREADER_ON = False


def _push(etype, code, value):
    """Push one raw event onto the bounded queue (drop-oldest under pressure)."""
    global _queue_dropped
    with _reader_lock:
        if len(_queue) >= _MAX_QUEUE:
            _queue.pop(0)
            _queue_dropped += 1
        _queue.append((etype, code, value))


# ---------------------------------------------------------------------------
# Log writer (called from the cadence loop — non-blocking drain)
# ---------------------------------------------------------------------------

def drain_events(now):
    """Drain the bounded queue into the per-data-type pad counters (FIX-C) and
    the EV activity signal. Called from the cadence loop each pass so the pad
    delivery evidence is counted even when the raw devices.py reader is not the
    active one. Never blocks (bounded drain)."""
    if not EVDEV_AVAILABLE or not S.EVREADER_ON:
        return
    global _queue_dropped
    global _queue
    items = []
    with _reader_lock:
        items = _queue
        _queue = []
        dropped = _queue_dropped
        _queue_dropped = 0
    if dropped:
        log(f"EVDEV: dropped {dropped} pad events under pressure "
            f"(bounded queue — delivery evidence preserved, detail shed)")
    for etype, code, value in items:
        if etype == 0x01:            # EV_KEY
            S.EV_ACT_COUNT += 1
            S.EV_ACT_LAST = now
            S.PAD_EV_COUNT += 1
            S.PAD_KEY_COUNT += 1
            S.PAD_KEY_LAST = now
            S.PAD_LAST_EV = now
        elif etype == 0x03:          # EV_ABS
            S.EV_ACT_COUNT += 1
            S.EV_ACT_LAST = now
            S.PAD_EV_COUNT += 1
            S.PAD_ABS_COUNT += 1
            S.PAD_LAST_EV = now
            if code in PAD_GYRO_AXES:
                S.PAD_GYRO_AXIS_COUNT += 1
                S.PAD_GYRO_LAST = now
            else:
                S.PAD_STICK_COUNT += 1
                S.PAD_STICK_LAST = now


# ---------------------------------------------------------------------------
# Public start (idempotent; called once from app.main)
# ---------------------------------------------------------------------------

def start_evreader():
    """Start the python-evdev pad reader thread once. Non-fatal if python-evdev
    is unavailable (the logger falls back to the raw devices.py capture).
    v3.12: S.EVREADER_ON is set True SYNCHRONOUSLY (before Thread.start) so that
    a scan_evdev running immediately after (app.main now starts us FIRST) sees
    the evreader as the pad owner and never opens the pad node raw — the
    exactly-one-owner invariant holds from the very first scan (evdev multicast
    would otherwise double-count every pad event)."""
    S.EVREADER_STARTED = True
    if not EVDEV_AVAILABLE:
        log("EVDEV: python-evdev NOT available in this interpreter — using the "
            "raw evdev capture (devices.py) for the pad hop")
        return
    try:
        S.EVREADER_ON = True   # pad ownership transfers to the evreader NOW
        threading.Thread(target=_run_reader, daemon=True).start()
        log("EVDEV: python-evdev pad reader thread started "
            f"(evdev {getattr(_evdev, '__version__', '?')})")
    except Exception as e:
        S.EVREADER_ON = False  # thread failed to start -> raw capture owns pad
        log(f"EVDEV: python-evdev reader thread start error: {e} "
            f"(using raw evdev capture)")
