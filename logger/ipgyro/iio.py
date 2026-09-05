# -*- coding: utf-8 -*-
"""
ipgyro.iio — IIO (IMU) gyro/accel discovery + sampling.

Also exposes two tiny shared format helpers used by hidraw.py too:
    _fmt(v)  -> "NA" for None, str(v) otherwise
    _mag(*v) -> sum of absolute non-None values
They live here (next to the IIO sampling that uses them most) and are imported
by value into ipgyro.hidraw — no circular import (iio never imports hidraw).

Shared mutable state (IIO_DEVICES/IIO_LAST_ACTIVE/IIO_LAST_IDLE/
IIO_FAST_UNTIL) is accessed ONLY as S.<name> (see ipgyro.state).
"""

import os
import time

from . import state as S
from .conf import (
    IIO_DIR,
    IIO_FAST_HOLD,
    IIO_FAST_INTERVAL,
    IIO_IDLE_HB,
    IIO_INTERVAL,
    IIO_RAW_ACCEL_RE,
    IIO_RAW_GYRO_RE,
)
from .logcore import log


def discover_iio():
    """Enumerate /sys/bus/iio/devices/iio:device* with gyro/accel raw channels
    and refresh the global IIO_DEVICES list."""
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

    old_ids = {d[0] for d in S.IIO_DEVICES}
    new_ids = {e for e, *_ in found}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    if added:
        log(f"IIO: discovered devices: {', '.join(added)}")
    if removed:
        log(f"IIO: devices gone: {', '.join(removed)}")
    S.IIO_DEVICES = found
    if not S.IIO_DEVICES:
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
    for entry, dpath, name, gyro, accel, scale in S.IIO_DEVICES:
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
                S.IIO_FAST_UNTIL = now + IIO_FAST_HOLD
            iv = IIO_FAST_INTERVAL if now < S.IIO_FAST_UNTIL else IIO_INTERVAL
            if now - S.IIO_LAST_ACTIVE.get(entry, 0.0) >= iv:
                log(line)
                S.IIO_LAST_ACTIVE[entry] = now
        else:
            if now - S.IIO_LAST_IDLE.get(entry, 0.0) >= IIO_IDLE_HB:
                log(line + " (idle)")
                S.IIO_LAST_IDLE[entry] = now
