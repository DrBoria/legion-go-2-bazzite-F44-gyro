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
line. v3.9 (todo 52f FIX-2): the matcher uses the REAL systemd-suspend/s2idle
phrases, and the monotonic wake-gap detector (obs.pm_wake_check) is gated on
S.SLEEP_PENDING so it can only backstop a journal-confirmed suspend — a plain
main-loop stall can never produce a sleep marker.

Shared mutable state (S.SLEEP_PENDING/S.SLEEP_WAKE_TS/S.STOP/PM_FOLLOW_LAST) is
accessed ONLY as S.<name>; constants imported by value from ipgyro.conf.
"""

import os
import select
import subprocess
import time

from . import state as S
from .conf import PM_FOLLOW_HB, PM_JOURNAL_UNIT
from .logcore import log, run_cmd


# v3.9 (todo 52f FIX-2 REAL SLEEP): the pre-52f matcher looked for
# "entering sleep state" / "suspending system" (enter) and "system resumed" /
# ("restored" + "sleep") (wake) — phrases systemd-sleep NEVER writes, so a real
# suspend/resume was never detected here and only the obs.pm_wake_check mono-gap
# backstop fired, emitting a FALSE [SLEEP-WAKE] every main-loop stall (~24-33 s
# periodic IIO/HIDFLOW stalls while the daemon threads stay alive).
# On this host (s2idle) systemd writes into `journalctl -u systemd-suspend`:
#   enter  "Starting systemd-suspend.service - System Suspend..."
#          systemd-sleep: "Successfully froze unit 'user.slice'."
#          systemd-sleep: "Performing sleep operation 'suspend'..."
#   wake   systemd-sleep: "System returned from sleep operation 'suspend'."
#          systemd-sleep: "Successfully thawed unit 'user.slice'."
#          "systemd-suspend.service: Deactivated successfully."
#          "Finished systemd-suspend.service - System Suspend."
# (the kernel additionally logs "PM: suspend entry/exit (s2idle)" in the whole
#  journal). Old phrases are kept as compat fallbacks.
_PM_ENTER = (
    "performing sleep operation",
    "starting systemd-suspend.service",
    "successfully froze unit",
    "suspend entry",
    "suspending system",
    "entering sleep state",
)
_PM_WAKE = (
    "system returned from sleep operation",
    "successfully thawed unit",
    "deactivated successfully",
    "finished systemd-suspend.service",
    "suspend exit",
    "system resumed",
    "resumed from suspend",
)


def _classify(line):
    """'enter' | 'wake' | None for one systemd-suspend journal line (matched
    against the REAL systemd/s2idle phrases listed above)."""
    low = line.lower()
    if any(p in low for p in _PM_WAKE):
        return "wake"
    if any(p in low for p in _PM_ENTER):
        return "enter"
    return None


def _wall_hms():
    """Current wall time as 'HH:MM:SS' (local)."""
    import datetime
    return datetime.datetime.now().strftime("%H:%M:%S")


def _capture_pre_sleep_baseline():
    """(v3.12 FIX-C) At [SLEEP-ENTER] (before the freeze) capture whether the
    pad buttons and the pad gyro axes were FLOWING, so the post-wake compare can
    honestly say "gyro reached the pad before sleep, and does/does not after
    wake while buttons keep flowing". Reads the current per-second pad counters
    (they are only reset by the next pad_verdict_scan, so they reflect the last
    second of real delivery)."""
    S.PRE_SLEEP_BTN = (S.PAD_KEY_COUNT > 0 or S.PAD_STICK_COUNT > 0)
    S.PRE_SLEEP_GYRO = S.PAD_GYRO_AXIS_COUNT > 0


def _pm_emit(line):
    """Classify one systemd-suspend journal line into [SLEEP-ENTER]/[SLEEP-WAKE]
    (both directions, boot-aware via the log() mono stamp). ONLY a journal-
    confirmed enter arms S.SLEEP_PENDING, so a mere process stall can never
    produce a sleep marker — the follower is the authoritative sleep source.
    v3.12 (todo 52r FIX-B): every sleep emits [SLEEP-ENTER]/[SLEEP-WAKE] with
    WALL timestamps + the sleep DURATION, and records the sleep state on S
    (entering/asleep/awake) for the SNAPSHOT sleep= field. The enter anchor is
    stamped the INSTANT the first authoritative signal fires (reliably BEFORE
    the process freezes). On wake we additionally stamp S.SLEEP_WAKE_TS so the
    main thread applies the wake consequences (snapshot + full capture) exactly
    once, and spawn the v3.10 post-wake dense capture thread (D2/D3/D5/D6) so
    the first seconds after resume are sampled INDEPENDENT of the cadence loop
    (a 24-33 s main-loop stall can no longer hide the wake transition)."""
    kind = _classify(line)
    if kind == "enter":
        if not S.SLEEP_PENDING:
            S.SLEEP_PENDING = True
            S.SLEEP_STATE = "entering"
            S.SLEEP_ENTER_MONO = time.monotonic()
            S.SLEEP_ENTER_WALL = _wall_hms()
            S.SLEEP_ENTER_LOG = S.SLEEP_ENTER_MONO
            S.WAKE_GYRO_STALLED = False
            _capture_pre_sleep_baseline()
            log(f"PM: [SLEEP-ENTER] wall={S.SLEEP_ENTER_WALL} " + line.strip())
    elif kind == "wake":
        if S.SLEEP_PENDING:
            S.SLEEP_PENDING = False
            S.SLEEP_WAKE_TS = time.monotonic()
            S.SLEEP_WAKE_WALL = _wall_hms()
            S.SLEEP_STATE = "awake"
            if S.SLEEP_ENTER_MONO is not None:
                S.SLEEP_LAST_DUR = S.SLEEP_WAKE_TS - S.SLEEP_ENTER_MONO
            dur = (f" dur={S.SLEEP_LAST_DUR:.1f}s"
                   if S.SLEEP_LAST_DUR is not None else "")
            log(f"PM: [SLEEP-WAKE] wall={S.SLEEP_WAKE_WALL}{dur} "
                + line.strip())
            # D2/D3/D5/D6 — dense post-wake capture in a daemon thread (the
            # journal follower is itself a daemon thread, so this is safe).
            try:
                from . import wake as W  # lazy: keep module-load order flat
                W.spawn_wake_burst(S.SLEEP_WAKE_TS)
            except Exception:
                pass


def pm_backlog():
    """One-shot recent systemd-suspend journal backlog SUMMARY, logged at
    startup so past suspends stay visible. v3.9 (todo 52f FIX-2): the backlog
    is HISTORICAL and logged as ONE summary line only — individual
    [SLEEP-ENTER]/[SLEEP-WAKE] markers are emitted ONLY for LIVE events by the
    follower / the gated wake backstop, NEVER replayed for old journal entries
    (replaying them at startup would look like fresh false wakes)."""
    from .logcore import run_cmd  # local import keeps module deps flat
    out = run_cmd(["journalctl", "-u", PM_JOURNAL_UNIT, "-n", "40",
                   "--no-pager", "-o", "short-iso"], timeout=8)
    n_enter = n_wake = 0
    if out:
        for line in out.splitlines():
            kind = _classify(line)
            if kind == "enter":
                n_enter += 1
            elif kind == "wake":
                n_wake += 1
    if n_enter or n_wake:
        log(f"PM: backlog — {n_enter} past suspend-enter + {n_wake} resume "
            f"journal line(s) in the recent {PM_JOURNAL_UNIT} history "
            f"(live [SLEEP-ENTER]/[SLEEP-WAKE] detection runs from here)")
    else:
        log("PM: no systemd-suspend journal backlog available")
    # a historical backlog must never leave a live sleep armed.
    S.SLEEP_PENDING = False
    S.SLEEP_WAKE_TS = None
    S.WAKE_HANDLED_TS = 0.0
    S.SLEEP_STATE = "awake"
    S.SLEEP_ENTER_MONO = None
    S.SLEEP_ENTER_WALL = ""
    S.SLEEP_LAST_DUR = None


# ---------------------------------------------------------------------------
# v3.12 (todo 52r FIX-B) — SECOND INDEPENDENT sleep signal: the kernel PM
# journal. The systemd-suspend unit follower is authoritative but can stall if
# journald restarts / the unit is renamed. This backstop polls the KERNEL PM
# lines ("PM: suspend entry/exit (s2idle)") on a short cadence and resolves a
# journal-confirmed enter (S.SLEEP_PENDING) that the unit follower missed, so
# the user's real sleep ALWAYS appears as [SLEEP-ENTER]/[SLEEP-WAKE]. It can
# NEVER emit a spurious enter (enter still requires S.SLEEP_PENDING to be set
# by the unit follower OR a kernel "suspend entry" line) and never duplicates.
# ---------------------------------------------------------------------------

def _kernel_classify(line):
    """'enter' | 'wake' | None for a KERNEL PM journal line (s2idle)."""
    low = line.lower()
    if "suspend exit" in low or "resume" in low and "suspend" in low:
        return "wake"
    if "suspend entry" in low or "pm: suspend entry" in low:
        return "enter"
    return None


def pm_kernel_poll(now):
    """(v3.12 FIX-B) Recovery-only poller for the SECOND sleep signal. Does
    nothing unless a journal-confirmed enter is pending (S.SLEEP_PENDING) — i.e.
    the pre-freeze follower armed a real sleep but its resume line was not
    consumed (follower restart / journald hiccup). It then scans the recent
    KERNEL PM journal for the WAKE that resolves it. It can NEVER emit a
    spurious [SLEEP-ENTER] (enter detection belongs to the pre-freeze follower)
    and never duplicates (once resolved pending is False, later polls are
    no-ops), so steady-state stalls produce nothing. Throttled by the caller."""
    if not S.SLEEP_PENDING:
        return
    from .conf import PM_KERNEL_WINDOW
    out = run_cmd(["journalctl", "-k", "-n", "60", "--no-pager",
                   "-o", "short-iso", f"--since=-{int(PM_KERNEL_WINDOW)}s"],
                  timeout=5)
    if not out:
        return
    for line in out.splitlines():
        if _kernel_classify(line) == "wake":
            _pm_emit(line)        # clears pending + stamps S.SLEEP_WAKE_TS
            return


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


def pm_kernel_loop():
    """(v3.12 FIX-B) SECOND INDEPENDENT sleep signal — follow the KERNEL PM
    journal (`journalctl -k -f`) so a real suspend is detected even if the
    systemd-suspend UNIT follower stalls (journald restart / unit rename). The
    kernel logs "PM: suspend entry/exit (s2idle)" on every real suspend. This
    follower classifies kernel PM lines and emits the same [SLEEP-ENTER]/
    [SLEEP-WAKE] markers (deduped against the unit follower via S.SLEEP_PENDING
    / S.SLEEP_ENTER_MONO). Runs in a daemon thread; non-blocking select() reads
    keep STOP responsive and auto-restart on EOF/read-error."""
    last_live = 0.0
    while not S.STOP:
        proc = None
        try:
            proc = subprocess.Popen(
                ["journalctl", "-k", "-f", "-n", "0", "--no-pager",
                 "-o", "short-iso"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"PM: cannot start kernel journalctl: {e} (retrying in 3s)")
            time.sleep(3)
            continue
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        log(f"PM: kernel PM journal follow started (pid={proc.pid}) — "
            f"second independent sleep signal")
        buf = b""
        while not S.STOP:
            now = time.monotonic()
            if now - last_live >= PM_FOLLOW_HB:
                last_live = now
                log(f"[PM-LIVE] kernel PM journal follower alive (pid={proc.pid})")
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
                log(f"PM: kernel PM journal follower read error: {ex}")
                break
            if not chunk:
                log("PM: kernel PM journal follower EOF "
                    "(journalctl exited / journald restarted)")
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace")
                if S.STOP:
                    break
                if _kernel_classify(line) is not None:
                    _pm_emit(line)
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
