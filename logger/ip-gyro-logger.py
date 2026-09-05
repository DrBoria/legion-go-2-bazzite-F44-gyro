#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ip-gyro-logger.py — thin launcher for the `ipgyro` package (todo 51c split).

The full passive diagnostic logger for the Legion Go 2 gyro patch now lives in
logger/ipgyro/ as a small, readable package:

    conf.py    constants / regexes / decode tables / boot-id reader
    state.py   every shared mutable global (accessed as S.<name>)
    logcore.py log(), log-file handle + lock, rotation, run_cmd
    devices.py /proc/bus/input/devices parsing + evdev capture + sessions
    iio.py     IIO gyro/accel discovery + sampling
    udev.py    udevadm monitor thread ([UDEV] lines)
    hidraw.py  deck + physical Legion hidraw monitor, per-report decode,
               HIDFLOW / FLOW GAP / ACTIVITY liveness
    obs.py     mode/attach state tracker, [SNAPSHOT], [FULL-ON], detach/dock,
               PM sleep/wake, game boundary
    ipj.py     InputPlumber journal mirror ([IPJ-LIVE]/[IPJ-DEAD])
    steam.py   Steam registry / controller_ui / procs ([STEAM-*] markers)
    app.py     main() — startup scans + cadence loop + cleanup

This file keeps the EXACT original filename/path so ip-gyro-logger.service and
install.sh keep working unchanged: it only adds this directory to sys.path,
imports main() from ipgyro.app and runs it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipgyro.app import main  # noqa: E402

if __name__ == "__main__":
    main()
