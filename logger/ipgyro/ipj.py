# -*- coding: utf-8 -*-
"""
ipgyro.ipj — the InputPlumber journal capture: the one-shot startup backlog
and the v3.5 watchdog journalctl -f follower thread ([IPJ-LIVE]/[IPJ-DEAD]).
Also captures the deck-uhid keep-alive line into S.DECK_KEEPALIVE so a
snapshot can prove whether the virtual deck target was re-created across a
detach/dock transition.

Shared mutable state (IPJ_FLOOD/DECK_KEEPALIVE/STOP) is accessed ONLY as
S.<name> (see ipgyro.state); constants + regex imported by value from
ipgyro.conf.
"""

import os
import select
import subprocess
import time

from . import state as S
from .conf import (
    IPJ_ADD_RE,
    IPJ_BACKLOG_LINES,
    IPJ_CREATE_RE,
    IPJ_DESTROY_RE,
    IPJ_LIVE_HB_S,
    IPJ_MAX_LINES_S,
    IPJ_RE,
    IPJ_REMOVE_RE,
    IPJ_RESTART_DELAY,
    IPJ_SILENT_COOLDOWN,
    IPJ_SUSPEND_RE,
    IPJ_WATCHDOG_S,
)
from .logcore import log, run_cmd


def _ipj_classify(line):
    """(R3) Classify an InputPlumber journal line into a greppable layer marker
    so the InputPlumber attach/detach/create/destroy/suspend decisions are
    visible as their own marker (not just a raw IPJ: mirror line). Returns a
    marker string or None. Priority: suspend > destroy > create > remove > add
    (a line rarely matches more than one, but the most specific wins)."""
    low = line.lower()
    if IPJ_SUSPEND_RE.search(low):
        return "[IPJ-SUSPEND]"
    if IPJ_DESTROY_RE.search(low):
        return "[IPJ-DESTROY]"
    if IPJ_CREATE_RE.search(low):
        return "[IPJ-CREATE]"
    if IPJ_REMOVE_RE.search(low):
        return "[IPJ-REMOVE]"
    if IPJ_ADD_RE.search(low):
        return "[IPJ-ADD]"
    return None


def _ipj_emit(line):
    # (Слой 1) remember the persist keep-alive so a snapshot can prove the
    # deck-uhid target was NEVER re-created across a detach/dock transition
    if "keeping target" in line and "deck-uhid" in line:
        S.DECK_KEEPALIVE = line.strip()
    wall = time.monotonic()
    if wall - S.IPJ_FLOOD[0] >= 1.0:
        S.IPJ_FLOOD[0] = wall
        S.IPJ_FLOOD[1] = 0
    if S.IPJ_FLOOD[1] >= IPJ_MAX_LINES_S:
        return
    S.IPJ_FLOOD[1] += 1
    # (R3) classify the InputPlumber layer decision into a greppable marker
    mk = _ipj_classify(line)
    if mk:
        log(f"{mk} " + line.strip())
    else:
        log("IPJ: " + line.strip())


def ipj_backlog():
    """One-shot recent journal backlog, logged at startup for context."""
    out = run_cmd(["journalctl", "-u", "inputplumber", "-n",
                   str(IPJ_BACKLOG_LINES), "--no-pager", "-o", "short-iso"],
                  timeout=10)
    if not out:
        log("IPJ: no inputplumber journal backlog available")
        return
    log("IPJ: --- backlog (last up to %d inputplumber journal lines) ---"
        % IPJ_BACKLOG_LINES)
    n = 0
    for line in out.splitlines():
        if IPJ_RE.search(line):
            _ipj_emit(line)
            n += 1
    log(f"IPJ: --- end backlog ({n} matching lines) ---")


def _inputplumber_alive():
    """G4: True when the REAL inputplumber process is alive (kill -0 on its
    actual PID). Prevents a false [IPJ-DEAD] when inputplumber is genuinely
    alive but simply idle (no journal lines for a long stretch)."""
    out = run_cmd(["pgrep", "-n", "-x", "inputplumber"])
    if not out:
        return False
    try:
        pid = int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ipj_tail_loop():
    """Follow `journalctl -u inputplumber -f`; log filtered lines. Runs in a
    daemon thread. (b) watchdog: non-blocking select() reads so EOF and SILENCE
    are both detected — [IPJ-DEAD] logs the CAUSE, then journalctl restarts
    after IPJ_RESTART_DELAY; [IPJ-LIVE] is emitted periodically while healthy.
    G4 (todo 52b): before declaring dead on a long SILENCE, verify the REAL
    inputplumber process is alive (kill -0). If it is alive and merely idle, we
    do NOT declare dead / restart — we just keep the mirror up and re-assert
    [IPJ-LIVE]. This removes the false [IPJ-DEAD] restarts while inputplumber
    is healthy but quiet."""
    last_line = time.monotonic()
    last_live = 0.0
    last_dead = 0.0
    while not S.STOP:
        proc = None
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", "inputplumber", "-f", "-n", "0",
                 "--no-pager", "-o", "short-iso"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"IPJ: cannot start journalctl: {e} (retrying in "
                f"{IPJ_RESTART_DELAY:.0f}s)")
            time.sleep(IPJ_RESTART_DELAY)
            continue
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        log(f"IPJ: journalctl -u inputplumber follow started (pid={proc.pid})")
        last_line = time.monotonic()
        buf = b""
        while not S.STOP:
            now = time.monotonic()
            silent = now - last_line
            # -- watchdog: EOF/read-error caught below; a long SILENCE while
            #    journalctl is still up means the mirror stalled (journald
            #    stopped delivering) -> declare dead. G4: only when the REAL
            #    inputplumber process is ALSO gone; if it is alive and merely
            #    idle, keep the mirror up (no false [IPJ-DEAD] / restart).
            if (silent >= IPJ_WATCHDOG_S
                    and now - last_dead >= IPJ_SILENT_COOLDOWN):
                if _inputplumber_alive():
                    # inputplumber is alive but quiet — not a real death
                    if now - last_live >= IPJ_LIVE_HB_S:
                        last_live = now
                        log(f"[IPJ-LIVE] inputplumber alive but idle "
                            f"(no journal line for {silent:.0f}s; real pid "
                            f"present — mirror kept up)")
                    last_line = time.monotonic()   # reset the silence window
                    continue
                last_dead = now
                if proc.poll() is None:
                    reason = (f"silent {silent:.0f}s "
                              f"(journalctl pid={proc.pid} still up — "
                              f"inputplumber process GONE, journald mirror "
                              f"stalled?)")
                else:
                    reason = f"journalctl exited (code={proc.returncode})"
                log(f"[IPJ-DEAD] inputplumber journal mirror {reason}; "
                    f"restarting in {IPJ_RESTART_DELAY:.0f}s")
                break
            # -- periodic liveness heartbeat -----------------------------------
            if silent < IPJ_WATCHDOG_S and now - last_live >= IPJ_LIVE_HB_S:
                last_live = now
                log(f"[IPJ-LIVE] inputplumber journal mirror alive "
                    f"(pid={proc.pid}, last line {silent:.0f}s ago)")
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
                log(f"[IPJ-DEAD] inputplumber journal mirror read error: {ex}")
                break
            if not chunk:
                log("[IPJ-DEAD] inputplumber journal mirror EOF "
                    "(journalctl exited / journald restarted)")
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace")
                last_line = time.monotonic()
                if S.STOP:
                    break
                if IPJ_RE.search(line):
                    _ipj_emit(line)
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
            time.sleep(IPJ_RESTART_DELAY)
