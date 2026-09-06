# -*- coding: utf-8 -*-
"""
ipgyro.logcore — the log core: the log-file handle + its lock, log(),
the signal handler, run_cmd() and the internal size rotation.

LOGFILE / LOG_LOCK intentionally stay PRIVATE to this module (they are the
"log handle core", per the split plan): only log(), maybe_rotate() and
app.main() (via `logcore.LOGFILE = open(...)`) ever touch them. Every other
piece of shared process state lives in ipgyro.state and is accessed as
S.<name> attributes so cross-module rebinding can never silently fork state.
"""

import datetime
import errno
import os
import subprocess
import threading
import time

from . import conf as C
from . import state as S

LOGFILE = None               # log file handle; None -> stdout only
LOG_LOCK = threading.Lock()  # so the udev thread never interleaves lines

# v3.10 (todo 52l, D1 ROOT-FIX): the ~24-33 s whole-process stalls (all daemon
# threads silent too — no [IPJ-LIVE]/[PM-LIVE] during the gap) are caused by
# log() writing the stdout MIRROR synchronously: under heavy full-capture
# output (gaming mode ~200 lines/s) the stdout pipe to systemd-journald fills,
# print(flush=True) blocks while holding LOG_LOCK, and EVERY thread (main loop
# + ipj + pm + udev) freezes until journald drains — a recurring 15-32 s freeze
# that looks like (and hides) the sleep-in-game wake transition. Fix: stdout is
# set O_NONBLOCK once and written with bounded non-blocking writes, so a full
# pipe DROPS mirror lines instead of freezing the process. The primary artifact
# /var/log/ip-gyro-logger.log is written unconditionally (never dropped), so no
# evidence is lost — only the journald mirror may shed lines under extreme flood.
_STDOUT_NB_DONE = False


def _ensure_stdout_nonblocking():
    """Set fd 1 (stdout -> journald) O_NONBLOCK once. Best-effort; a TTY or an
    already-nonblocking fd is fine. Never raises."""
    global _STDOUT_NB_DONE
    if _STDOUT_NB_DONE:
        return
    _STDOUT_NB_DONE = True
    try:
        import fcntl
        fl = fcntl.fcntl(1, fcntl.F_GETFL)
        fcntl.fcntl(1, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    except Exception:
        pass


def _write_stdout_bounded(data):
    """Write all of `data` (bytes) to fd 1 with a bounded non-blocking loop.
    Stops (dropping the remainder) on EAGAIN/EWOULDBLOCK — a full journald pipe
    can therefore never block the logger. Never raises."""
    _ensure_stdout_nonblocking()
    view = data
    while view:
        try:
            n = os.write(1, view)
        except BlockingIOError:
            break
        except OSError as ex:
            if ex.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                break
            raise
        except Exception:
            break
        if not n:
            break
        view = view[n:]


def log(msg):
    """Timestamped (local time) line WITH a boot-anchored monotonic stamp,
    written to BOTH stdout (-> journald) and the log file. Never raises, never
    blocks, always flushed. (a) The stamp is appended AFTER msg so every
    existing field parser ($1=date $2=time $3=TYPE) keeps working; the ordering
    key [boot=<boot_id> mono=<secs>] makes the timeline immune to wall-clock
    inversions (the pre-reboot +2h clock bug can never scramble it again)."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    mono = time.monotonic() - S.BOOT_T0
    line = f"{ts} {msg} [boot={S.BOOT_ID} mono={mono:011.3f}]"
    data = (line + "\n").encode("utf-8", "replace")
    with LOG_LOCK:
        try:
            _write_stdout_bounded(data)
        except Exception:
            pass
        if LOGFILE is not None:
            try:
                LOGFILE.write(line + "\n")
                LOGFILE.flush()
            except Exception:
                pass


def _signal_handler(signum, _frame):
    """Convert SIGINT/SIGTERM into a clean shutdown instead of an abrupt exit."""
    if not S.STOP:
        log(f"LOGGER: received signal {signum}, shutting down cleanly")
    S.STOP = True


def run_cmd(cmd, timeout=5):
    """Run a command; return stripped stdout ('' on failure / not-found)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def maybe_rotate():
    """(c) Internal size rotation: above LOGGER_MAX_BYTES close the log, rename
    to <path>.1 (os.replace overwrites), reopen a fresh file, emit [ROTATE].
    The external logrotate stays only as a backstop — a 968MB file must never
    happen again."""
    global LOGFILE
    if LOGFILE is None:
        return
    try:
        cur = os.fstat(LOGFILE.fileno()).st_size
    except Exception:
        return
    if cur < C.LOGGER_MAX_BYTES:
        return
    path = getattr(LOGFILE, "name", None)
    if not path:
        return
    rotated = False
    with LOG_LOCK:
        try:
            LOGFILE.flush()
            LOGFILE.close()
        except Exception:
            pass
        LOGFILE = None
        try:
            os.replace(path, path + ".1")
            rotated = True
        except Exception:
            rotated = False
    if rotated:
        try:
            LOGFILE = open(path, "a", buffering=1)
        except Exception as e:
            LOGFILE = None
            log(f"LOGGER: [ROTATE] reopen {path} failed: {e}")
            return
        log(f"LOGGER: [ROTATE] rotated {cur} bytes -> {path}.1; "
            f"fresh {path} open")
    else:
        try:
            LOGFILE = open(path, "a", buffering=1)
        except Exception:
            LOGFILE = None
        log(f"LOGGER: [ROTATE] rename {path} -> {path}.1 failed; "
            f"reopened same file")
