# -*- coding: utf-8 -*-
"""
ipgyro.pm — the reliable PM (power-management) sleep/wake journal follower.

Why this module exists (todo 51d, R6)
-------------------------------------
The old pm_probe() (obs.py) only POLLED `journalctl -n 25 --since=-45s` on a
5 s cadence. Because the logger process FREEZES during suspend, the poll could
never see the "entering sleep state" line BEFORE the freeze — [SLEEP-ENTER] was
only ever reconstructed AFTER wake, out of order with [SLEEP-WAKE]. This module
adds a dedicated `journalctl -u systemd-suspend -f` follower thread (mirroring
the IPJ watchdog pattern) so [SLEEP-ENTER] is emitted the INSTANT systemd logs
it, i.e. reliably BEFORE the process freezes, and [SLEEP-WAKE] on the resume
line. A monotonic wake-gap detector (obs.pm_wake_check) stays as a backstop for
resumes that journald does not attribute to systemd-suspend.

Shared mutable state (S.SLEEP_PENDING/S.STOP/PM_FOLLOW_LAST) is accessed ONLY
as S.<name>; constants imported by value from ipgyro.conf.
"""

import os
import select
import subprocess
import time

from . import state as S
from .conf import PM_FOLLOW_HB, PM_JOURNAL_UNIT
from .logcore import log


def _pm_emit(line):
    """Classify one systemd-suspend journal line into [SLEEP-ENTER]/[SLEEP-WAKE]
    (both directions, boot-aware via the log() mono stamp)."""
    low = line.lower()
    if "entering sleep state" in low or "suspending system" in low:
        if not S.SLEEP_PENDING:
            S.SLEEP_PENDING = True
            log("PM: [SLEEP-ENTER] " + line.strip())
    elif "system resumed" in low or ("restored" in low and "sleep" in low):
        if S.SLEEP_PENDING:
            S.SLEEP_PENDING = False
            log("PM: [SLEEP-WAKE] " + line.strip())


def pm_backlog():
    """One-shot recent systemd-suspend journal backlog, logged at startup so a
    sleep that happened just before the logger started is still visible."""
    from .logcore import run_cmd  # local import keeps module deps flat
    out = run_cmd(["journalctl", "-u", PM_JOURNAL_UNIT, "-n", "40",
                   "--no-pager", "-o", "short-iso"], timeout=8)
    if not out:
        log("PM: no systemd-suspend journal backlog available")
        return
    log("PM: --- backlog (last systemd-suspend journal lines) ---")
    n = 0
    for line in out.splitlines():
        if ("entering sleep state" in line.lower()
                or "system resumed" in line.lower()):
            _pm_emit(line)
            n += 1
    log(f"PM: --- end backlog ({n} matching lines) ---")


def pm_journal_loop():
    """Follow `journalctl -u systemd-suspend -f`; emit [SLEEP-ENTER]/[SLEEP-WAKE]
    the moment systemd logs them (reliable pre-freeze enter detection). Runs in
    a daemon thread. Non-blocking select() reads keep STOP responsive and detect
    EOF/read-error so the follower restarts instead of silently dying."""
    last_line = time.monotonic()
    last_live = 0.0
    while not S.STOP:
        proc = None
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", PM_JOURNAL_UNIT, "-f", "-n", "0",
                 "--no-pager", "-o", "short-iso"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"PM: cannot start journalctl: {e} (retrying in 3s)")
            time.sleep(3)
            continue
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        log(f"PM: journalctl -u {PM_JOURNAL_UNIT} follow started "
            f"(pid={proc.pid})")
        last_line = time.monotonic()
        buf = b""
        while not S.STOP:
            now = time.monotonic()
            # -- periodic liveness heartbeat -----------------------------------
            if now - last_live >= PM_FOLLOW_HB:
                last_live = now
                log(f"[PM-LIVE] systemd-suspend journal follower alive "
                    f"(pid={proc.pid}, last line {now - last_line:.0f}s ago)")
            # -- non-blocking read (1s select timeout keeps STOP responsive) ---
            try:
                r, _, _ = select.select([fd], [], [], 1.0)
            except InterruptedError:
                continue
            if not r:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as ex:
                log(f"PM: systemd-suspend journal follower read error: {ex}")
                break
            if not chunk:
                log("PM: systemd-suspend journal follower EOF "
                    "(journalctl exited / journald restarted)")
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace")
                last_line = time.monotonic()
                if S.STOP:
                    break
                _pm_emit(line)
        # -- teardown journalctl ------------------------------------------------
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass
        if not S.STOP:
            time.sleep(3)
