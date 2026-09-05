# -*- coding: utf-8 -*-
"""
ipgyro.gyro — the gyro-source observability helpers (todo 51d, R1 + R9).

R1 — ATTACHED CENTER body IMU identity
--------------------------------------
The logger samples every iio device generically (`IIO iio:deviceN ...`), but
the user must be able to tell WHICH iio device is the ATTACHED CENTER body IMU
(the sensor InputPlumber binds as the center / right_gyro slot source — on the
Legion Go 2 that is `iio:device2` named `gyro_3d`, syspath
`.../HID-SENSOR-200076.2.auto/iio:device2`). center_imu_scan() identifies that
device from the discovered iio list and emits a [CEN-IMU] marker carrying its
source identity (entry/name/syspath) so the IIO sample stream can be correlated
with the InputPlumber [gyro-center]/[CEN-GYRO] journal markers.

R9 — gyro input-source switching (which source became active + WHY)
-------------------------------------------------------------------
The logger only ever SEES the effect of a gyro source switch (IIO lines vs
LEGION-SRC XInput gyro bytes). gyro_src_scan() reads a recent window of the
InputPlumber journal and mirrors the lines that DECLARE the switch
(gyro_center arbiter / gyro-handle / force_iio / iio center binding) as a
[GYRO-SRC] marker, so the timeline shows WHICH source became active and WHY.
It also emits a periodic [GYRO-SRC] verdict derived from the live observed
state (center IIO present+active vs LEGION-SRC XInput gyro present) so the
active source is always stated even when InputPlumber logs nothing.

Shared mutable state (S.IIO_DEVICES/S.CEN_IMU/S.GYRO_SRC/S.GYRO_SRC_LAST/
S.HID_DEVICES/S.STOP) is accessed ONLY as S.<name>; constants imported by value
from ipgyro.conf.
"""

import re
import time

from . import state as S
from . import discover as D
from .conf import (
    CENTER_IMU_NAME_HINTS,
    GYRO_JOURNAL_LINES,
    GYRO_JOURNAL_S,
    GYRO_SRC_RE,
)
from .logcore import log, run_cmd


# ---------------------------------------------------------------------------
# R1 — center body IMU identity
# ---------------------------------------------------------------------------

def _center_imu_from_list():
    """Pick the iio device that is the ATTACHED CENTER body IMU from the current
    S.IIO_DEVICES list. Heuristic: prefer a device whose name matches a center
    gyro hint (gyro_3d/gyro/imu); fall back to the first gyro-capable device.
    Returns a dict {entry,name,dpath} or None."""
    gyro_caps = []
    for entry, dpath, name, gyro, accel, scale in S.IIO_DEVICES:
        nm = (name or "").lower()
        if any(h in nm for h in CENTER_IMU_NAME_HINTS):
            return {"entry": entry, "name": name or "?", "dpath": dpath}
        if gyro:
            gyro_caps.append({"entry": entry, "name": name or "?",
                              "dpath": dpath})
    if gyro_caps:
        return gyro_caps[0]
    return None


def center_imu_scan(now):
    """Identify the center body IMU from the discovered iio devices and emit a
    [CEN-IMU] marker (change-only + periodic re-assert) carrying its source
    identity so the IIO sample stream correlates with [gyro-center]/[CEN-GYRO]."""
    cen = _center_imu_from_list()
    if cen is None:
        if S.CEN_IMU is not None:
            S.CEN_IMU = None
            log("[CEN-IMU] center body IMU GONE (no gyro/imu iio device "
                "discovered)")
        return
    changed = (S.CEN_IMU is None
               or S.CEN_IMU.get("entry") != cen["entry"])
    if changed:
        S.CEN_IMU = cen
        log(f"[CEN-IMU] attached center body IMU = {cen['entry']} "
            f"name={cen['name']} syspath={cen['dpath']} "
            f"(correlate with [gyro-center]/[CEN-GYRO] journal markers)")
    elif now - S.CEN_IMU_LAST >= 60.0:
        S.CEN_IMU_LAST = now
        log(f"[CEN-IMU] (still) attached center body IMU = {cen['entry']} "
            f"name={cen['name']} syspath={cen['dpath']}")


# ---------------------------------------------------------------------------
# R9 — gyro source arbiter verdict + journal-window reader
# ---------------------------------------------------------------------------

def _observed_gyro_sources():
    """Derive which gyro sources are currently OBSERVED live (role-based):
      * center IIO present (S.CEN_IMU) -> 'center-iio'
      * physical-source XInput gyro present (role, not pid) -> 'physical-xinput'
    Returns a deduplicated list of active source labels (a composite device can
    expose several physical-source hidraw interfaces, but it is ONE gyro
    source)."""
    srcs = []
    if S.CEN_IMU is not None:
        srcs.append(f"center-iio:{S.CEN_IMU['entry']}")
    if any(e.get("role") == D.ROLE_PHYSICAL
           for e in S.HID_DEVICES.values()):
        srcs.append("physical-xinput")
    return srcs


def _gyro_src_verdict():
    """A compact human verdict of the active gyro source + why, from the live
    observed state (used for the periodic [GYRO-SRC] re-assert)."""
    srcs = _observed_gyro_sources()
    if not srcs:
        return "none (no gyro source observed)"
    if len(srcs) == 1:
        return f"{srcs[0]} (only source present)"
    # both present: the center IIO is the arbiter-preferred source on the
    # Legion Go 2 (gyro_center proxy); the XInput path is the fallback.
    return ("center-iio preferred (gyro_center arbiter); "
            + " + ".join(srcs))


def gyro_src_scan(now):
    """(R9) Mirror InputPlumber journal lines that DECLARE a gyro source switch
    as [GYRO-SRC], and periodically re-assert the observed-source verdict."""
    # -- journal-window reader: mirror lines that declare the switch ----------
    out = run_cmd(["journalctl", "-u", "inputplumber", "-n",
                   str(GYRO_JOURNAL_LINES), "--no-pager", "-o", "short-iso",
                   f"--since=-{int(GYRO_JOURNAL_S)}s"], timeout=8)
    if out:
        for line in out.splitlines():
            if GYRO_SRC_RE.search(line):
                _mirror_gyro_src(line)
    # -- periodic observed-source verdict (change-only + re-assert) -----------
    verdict = _gyro_src_verdict()
    if verdict != S.GYRO_SRC:
        S.GYRO_SRC = verdict
        log(f"[GYRO-SRC] active={verdict}")
    elif now - S.GYRO_SRC_LAST >= 30.0:
        S.GYRO_SRC_LAST = now
        log(f"[GYRO-SRC] (still) active={verdict}")


def _mirror_gyro_src(line):
    """Emit one [GYRO-SRC] marker mirroring an InputPlumber journal line that
    declares a gyro source switch (deduplicated per identical line)."""
    key = line.strip()
    # dedupe: only log a given journal line once per 30 s window
    if S.GYRO_SRC_SEEN == key:
        return
    S.GYRO_SRC_SEEN = key
    log("[GYRO-SRC] " + key)
