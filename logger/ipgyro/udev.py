# -*- coding: utf-8 -*-
"""
ipgyro.udev — the udevadm monitor daemon thread (auto-restarting). Watches
input/hidraw/hid/usb/iio add/remove uevents and emits ONE clean [UDEV] line per
RELEVANT uevent so a physical detach/dock or an XInput<->DInput mode switch is
visible at the transport level.

Interest is DYNAMIC (todo 52a C3): an event is relevant when its DEVPATH /
identity matches a node the GENERIC role registry currently captures (a
physical-source or deck-output), or when it is a virtual (uhid/vhci) or
gamepad-class device — NOT a hard-coded vendor whitelist. The [UDEV] line
carries the observed identity (vid/pid) + the role it maps to when known.

Only reads the shutdown flag (S.STOP); all other mutable state lives in
ipgyro.state.
"""

import re
import subprocess
import time

from . import state as S
from . import discover as D
from .conf import UDEVADM_CMD
from .logcore import log


def _vidpid_of(prop):
    """(vid, pid) observed identity from a uevent, or (None, None)."""
    try:
        vid = prop.get("ID_VENDOR_ID", "")
        pid = prop.get("ID_MODEL_ID", "")
        if vid and pid:
            return vid.lower(), pid.lower()
    except Exception:
        pass
    m = re.search(r"(?:0003:)?([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})",
                  prop.get("DEVPATH", "") or "")
    if m:
        return m.group(1).lower(), m.group(2).lower()
    return None, None


def _role_for(vid, pid):
    """Map an observed (vid,pid) to the role of a currently-open node, if any.
    vid/pid are OBSERVABLE identity — used only to correlate a uevent with the
    registry entry that is already open, never as a selector."""
    if not vid:
        return None
    for e in S.HID_DEVICES.values():
        if (e.get("vid") or "").lower() == vid and (e.get("pid") or "").lower() == pid:
            return e.get("role")
    return None


def _udev_interesting(prop):
    """True if a udev uevent is relevant: it matches a currently-open role node
    (physical-source / deck-output) by observed identity, OR it is a virtual
    (uhid/vhci) or gamepad-class device. No hard-coded vendor whitelist."""
    vid, pid = _vidpid_of(prop)
    if vid and _role_for(vid, pid):
        return True
    devpath = (prop.get("DEVPATH", "") or "").lower()
    if "/uhid/" in devpath or "/vhci_hcd/" in devpath or "/usbip" in devpath:
        return True
    # gamepad-class: a jsN handler or a gamepad-named device in the uevent
    name = (prop.get("NAME", "") or "").lower()
    if any(tok in name for tok in ("gamepad", "controller", "x-box", "deck")):
        return True
    return False


def udev_monitor_loop():
    """Watch udev for input/hidraw/hid/usb/iio add/remove uevents. Emits ONE
    clean [UDEV] line per relevant uevent so a physical detach/dock or an
    XInput<->DInput mode switch is visible at the transport level with its
    action/devname/subsystem/role. Restarts udevadm if it exits unexpectedly.
    Runs in a daemon thread."""
    while not S.STOP:
        proc = None
        try:
            proc = subprocess.Popen(UDEVADM_CMD, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception as e:
            log(f"UDEV: cannot start udevadm monitor: {e} (retrying in 3s)")
            time.sleep(3)
            continue
        log(f"UDEV: udevadm monitor started (pid={proc.pid})")

        def flush(ev):
            if not ev or not _udev_interesting(ev):
                return
            action = ev.get("ACTION", "?")
            devname = ev.get("DEVNAME", "-")
            subsys = ev.get("SUBSYSTEM", "?")
            vid, pid = _vidpid_of(ev)
            role = _role_for(vid, pid) or ""
            who = f" role={role}" if role else ""
            log(f"[UDEV] action={action} devname={devname} subsystem={subsys} "
                f"vid={vid or ''} pid={pid or ''}{who} "
                f"devpath={ev.get('DEVPATH', '')}")
            # (v3.8 F2) during the startup blind window (S.TRACK_LIVE still
            # False = the main loop has not gone live yet) record physical-
            # source churn so obs.startup_reconcile() can synthesize the
            # [DETACH]/[DOCK] markers the init-gap scan never diffed. Debounced:
            # a redock burst (add/remove/add within ~1.5 s) collapses into ONE
            # detach + ONE dock event.
            if (not S.TRACK_LIVE and role == D.ROLE_PHYSICAL
                    and action in ("add", "remove")
                    and len(S.STARTUP_CHURN) < 32):
                kind = "dock" if action == "add" else "detach"
                _ts = time.monotonic()
                prev = S.STARTUP_CHURN[-1] if S.STARTUP_CHURN else None
                if prev is None or (prev["kind"] != kind
                                    and _ts - prev["ts"] >= 1.5):
                    S.STARTUP_CHURN.append({"kind": kind, "ts": _ts})

        try:
            ev = {}
            for line in proc.stdout:
                if S.STOP:
                    break
                line = line.rstrip("\n")
                if not line:
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    ev[k.strip()] = v.strip()
                else:
                    # a new "UDEV [ts] action devpath (subsystem)" header closes
                    # the previous uevent's property block
                    flush(ev)
                    ev = {}
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
        if not S.STOP:
            log("UDEV: udevadm monitor exited unexpectedly — restarting in 3s")
            time.sleep(3)
