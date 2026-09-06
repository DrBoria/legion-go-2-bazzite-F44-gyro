# -*- coding: utf-8 -*-
"""
ipgyro.app — the entry orchestrator: _log_startup_info() and main() (argparse,
log-file open, signal handlers, udev thread, startup scans/logs incl. the
v3.5 active line, and the cadence loop with rotation / pm-wake / iio full-mode
gating / steam-state cadence / pm probe / steam scan / per-second evdev+hidraw
capture, then finally cleanup). The thin launcher logger/ip-gyro-logger.py
does `sys.path.insert(0, ...)` + `from ipgyro.app import main` and calls it.

The log-file handle is opened HERE through `logcore.LOGFILE = ...` (the log
core owns the handle/lock); every other shared mutable global is read/written
as S.<name>.
"""

import argparse
import os
import platform
import signal
import sys
import threading
import time

from . import conf as C
from . import config as CFG
from . import discover as D
from . import logcore as LC
from . import state as S
from .devices import (
    build_node_map,
    device_set_id,
    handle_evdev,
    log_device_inventory,
    log_snapshot,
    parse_proc_input_devices,
    scan_evdev,
    session_scan,
)
from .gyro import center_imu_scan, gyro_src_scan
from .hidraw import emit_hid_summary, handle_hidraw, scan_hidraw
from .iio import discover_iio, sample_iio
from .ipj import ipj_backlog, ipj_tail_loop
from .logcore import _signal_handler, log, maybe_rotate, run_cmd
from .obs import (_full_mode, loop_stall_watchdog_loop, mode_scan,
                  pad_verdict_scan, pm_probe, pm_wake_check, startup_reconcile,
                  state_tracker)
from .pm import pm_backlog, pm_journal_loop, pm_kernel_loop, pm_kernel_poll
from .evreader import drain_events, start_evreader
from .steam import steam_scan, steam_state_verdict
from .udev import udev_monitor_loop


# ---------------------------------------------------------------------------
# Config load (todo 52a/52b) — single generic config, applied once at startup.
# ---------------------------------------------------------------------------

def _load_config():
    """Load /etc/ip-gyro-logger/config.toml (or the env override), store the
    effective Cfg on S.CFG and the resolved [labels] on S.LABELS, and apply the
    tunable cadences/sizes as C.<name> overrides so the runtime reads config.
    Missing/broken config -> strict defaults + one loud line. Never crashes."""
    cfg = CFG.load()
    S.CFG = cfg
    S.LABELS = cfg.labels
    if cfg.status == "loaded":
        log(f"LOGGER: config loaded from {cfg.path}")
    elif cfg.status == "missing":
        log(f"LOGGER: config not found ({cfg.path}), using defaults")
    else:
        log(f"LOGGER: config invalid ({cfg.path}), using defaults")
    # -- apply generic tunables as conf-attribute overrides -------------------
    def _apply(section, mapping):
        for attr, key in mapping.items():
            try:
                setattr(C, attr, cfg.num(section, key))
            except Exception:
                pass
    _apply("cadence", {
        "SNAP_INTERVAL": "snap", "HB_INTERVAL": "hb", "IIO_INTERVAL": "iio",
        "IIO_IDLE_HB": "iio_idle", "IIO_FAST_INTERVAL": "iio_fast",
        "IIO_FAST_HOLD": "iio_fast_hold", "IIO_RESCAN_INTERVAL": "iio_rescan",
        "SESS_INTERVAL": "sess", "EV_THROTTLE": "ev_throttle",
        "HID_FLOW_INTERVAL": "flow", "HID_RAW_SAMPLE": "hid_raw",
        "STEAM_SCAN_INTERVAL": "steam_scan", "STEAM_STATE_INTERVAL": "steam_state",
        "IPJ_BACKLOG_LINES": "ipj_backlog", "IPJ_MAX_LINES_S": "ipj_max_s",
        "IPJ_WATCHDOG_S": "ipj_watchdog", "IPJ_LIVE_HB_S": "ipj_live",
        "GYRO_SRC_INTERVAL": "gyro_src", "DEV_INV_HB": "dev_inv",
        "PM_FOLLOW_HB": "pm_follow",
        # v3.10 (todo 52l) sleep-in-game visibility tunables (D1-D7)
        "WAKE_BURST_S": "wake_burst", "WAKE_BURST_IV": "wake_burst_iv",
        "WAKE_REASSERT_S": "wake_reassert", "REGRAB_MISSING_S": "regrab_missing",
        "LOOP_STALL_LOG_S": "loop_stall_log", "LOOP_STALL_HB": "loop_stall_hb",
        "IPJ_WAKE_FLUSH_S": "ipj_wake_flush",
        # v3.11 (todo 52o) real input-delivery tracking cadences
        "PAD_STALL_S": "pad_stall", "PAD_DELIVERY_HB": "pad_delivery_hb",
        # v3.12 (todo 52r) truthful mode/sleep/per-source cadences
        "MODE_SCAN_INTERVAL": "mode_scan", "PM_KERNEL_POLL": "pm_kernel_poll",
        "PM_KERNEL_WINDOW": "pm_kernel_window",
        "WAKE_GYRO_COMPARE_S": "wake_gyro_compare",
    })
    # D3 — the two [GYRO-FEED] verdict instants come from two config keys.
    try:
        C.GYRO_FEED_S = (float(cfg.num("cadence", "gyro_feed_1")),
                         float(cfg.num("cadence", "gyro_feed_2")))
    except Exception:
        pass
    _apply("full_mode", {
        "FULL_MODE_POST": "post", "FULL_MODE_WAKE": "wake",
        "FULL_MODE_GAME": "game",
    })
    try:
        C.LOGGER_MAX_BYTES = int(cfg.num("log", "max_bytes"))
    except Exception:
        pass
    return cfg


# ---------------------------------------------------------------------------
# Startup info
# ---------------------------------------------------------------------------

def _log_startup_info():
    """Log OS/host info, inputplumber service state and its cmdline."""
    un = platform.uname()
    log(f"LOGGER: uname system={un.system} release={un.release} "
        f"version={un.version} machine={un.machine}")
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    log(f"LOGGER: os-release {line.split('=', 1)[1].strip().strip(chr(34))}")
                    break
    except Exception:
        pass
    active = run_cmd(["systemctl", "is-active", "inputplumber"])
    log(f"LOGGER: systemctl is-active inputplumber = {active or 'unknown'}")
    pid = run_cmd(["pgrep", "-n", "inputplumber"])
    if pid:
        cmdline = None
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            pass
        log(f"LOGGER: inputplumber pid={pid} cmdline={cmdline or '(unreadable)'}")
    else:
        log("LOGGER: inputplumber process NOT running")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Passive diagnostic logger for the Legion Go 2 gyro patch "
                    "(InputPlumber -> virtual Steam Deck controller).")
    ap.add_argument("--log", default="/var/log/ip-gyro-logger.log",
                    help="log file path (default: %(default)s); output is also "
                         "mirrored to stdout for journalctl")
    args = ap.parse_args()

    # Open the log file BEFORE anything is logged; failure degrades to stdout-only.
    try:
        LC.LOGFILE = open(args.log, "a", buffering=1)
    except Exception as e:
        LC.LOGFILE = None
        print(f"WARNING: cannot open log file {args.log}: {e} "
              f"(continuing with stdout only)", flush=True)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log(f"LOGGER: {C.PROG} starting (pid={os.getpid()} cmd={' '.join(sys.argv)})")
    _log_startup_info()
    # (todo 52a/52b) single generic config: load once, apply tunables + labels.
    _load_config()

    # udev monitor in a daemon thread (auto-restarts if udevadm dies).
    threading.Thread(target=udev_monitor_loop, daemon=True).start()

    devices = parse_proc_input_devices()
    set_id = device_set_id(devices)
    log_snapshot(devices, full=True)
    # (R4) full device inventory with VID:PID/serial/syspath/class at startup
    log_device_inventory(devices, "startup")
    # (v3.12 PART 2) python-evdev pad reader (independent, churn-following).
    # Started BEFORE the raw scan: start_evreader() sets S.EVREADER_ON True
    # synchronously, so the scan_evdev right after already EXCLUDES the pad node
    # from the raw capture — the pad is owned by exactly ONE reader from the
    # very first scan (evdev multicast would otherwise double-count pad events).
    # Falls back to the raw devices.py capture if python-evdev is unavailable.
    start_evreader()
    scan_evdev(devices, build_node_map(devices))
    discover_iio()
    # (R1) identify the attached center body IMU right after iio discovery
    center_imu_scan(time.monotonic())

    # v2: open hidraw sources (generic role registry), snapshot Steam state,
    # pull an InputPlumber journal backlog, then start the journal follower.
    found_hid = scan_hidraw()
    # (todo 52a) prove roles were assigned by capability, not hard-coded ids
    D.snapshot_roles(S.LABELS, log_banner=True)
    state_tracker(devices, found_hid)
    steam_scan(time.monotonic())
    ipj_backlog()
    threading.Thread(target=ipj_tail_loop, daemon=True).start()
    # (R6) reliable systemd-suspend journal follower for [SLEEP-ENTER]/[SLEEP-WAKE]
    pm_backlog()
    threading.Thread(target=pm_journal_loop, daemon=True).start()
    # (v3.12 FIX-B) SECOND INDEPENDENT sleep signal: kernel PM journal follower
    # (catches a real suspend even if the systemd-suspend unit follower stalls).
    threading.Thread(target=pm_kernel_loop, daemon=True).start()
    # (D7) loop-stall watchdog thread: reports [LOOP-STALL] when the cadence
    # loop produces nothing for >LOOP_STALL_LOG_S (a stall is logged as a cause
    # rather than appearing only as missing data). Arm S.LAST_TICK NOW (before
    # the thread starts) so the startup codec-sniff window (~30s before the
    # cadence loop's first tick) cannot look like a 60000s+ silence and fire a
    # spurious [LOOP-STALL].
    S.LAST_TICK = time.monotonic()
    threading.Thread(target=loop_stall_watchdog_loop, daemon=True).start()
    log("LOGGER: v2 channels active — generic role registry (physical-source + "
        "deck-output by capability/transport), InputPlumber journal, Steam "
        "registry + controller log")
    log("LOGGER: v3.1 decode active — 64-byte deck reports decoded per report "
        "(DECODE/MOTION), cross-direction ACTIVITY + FLOW GAP loss markers")
    log("LOGGER: v3.1 Steam Input activation tracking — controller_ui focus "
        "(STEAM UI / STEAM UI FOCUS) + running-game AppID (STEAM PROC) — "
        "direction D (Steam -> game)")
    log("LOGGER: v3.2 active — physical XInput IMU decode (full raw hex), deck "
        "raw24-35 correlation, coalesced DECK bursts, fast IIO sampling during "
        "motion")
    log("LOGGER: v3.3 active — EVERY field of the physical XInput frame decoded "
        "(DECODE ... XFULL: enums/batteries/sticks/buttons/triggers/touch/"
        "lq-gyro/IMU-timestamps/both accel+gyro/trailer) — full line on any "
        "change + decoded keepalive every 5 s with the raw-hex snapshot")
    log("LOGGER: v3.4 detach/dock markers active — [UDEV] per-uevent "
        "(action/devname/subsystem/role), [DETACH]/[DOCK] physical source "
        "gone/back, [DECK-DEAD]/[DECK-ALIVE] and [LEGION-DEAD]/[LEGION-ALIVE] "
        "flow-drop/resume — one loud line per transition, no periodic spam")
    log(f"LOGGER: {C.LOG_VERSION} V22 full-observability active — "
        f"[boot={S.BOOT_ID} mono=...] on every line, IPJ watchdog "
        f"[IPJ-LIVE]/[IPJ-DEAD], size rotation [ROTATE], "
        f"[SNAPSHOT] before/after transitions, [FULL-ON] capture windows, "
        f"[STEAM-STATE]/[STEAM-CONFIG]/[STEAM-GRAB/RELEASE/REGRAB], "
        f"[GAME-START]/[GAME-STOP], [SLEEP-ENTER]/[SLEEP-WAKE], "
        f"[RECONNECT-DT]")
    log("LOGGER: v3.6 todo-51d 'ЛОГ ВСЁ' audit active — [CEN-IMU] center body "
        "IMU identity, [GYRO-SRC] source-switch verdict, [DEV-INV] full device "
        "inventory (VID:PID/serial/syspath/class), [FRAME] full-mode per-frame "
        "capture, [IPJ-ADD/REMOVE/CREATE/DESTROY/SUSPEND] InputPlumber layer, "
        "[GAME-BIND]/[GAME-LOSS] had/lost semantics, [REGRAB-DT] dock->re-bind "
        "latency, reliable [SLEEP-ENTER]/[SLEEP-WAKE] systemd-suspend follower")
    log("LOGGER: v3.7 todo-52b generic refactor active — single config "
        f"({CFG.CONFIG_PATH}), role+codec discovery (no device ids), "
        "G1 Steam-side grab/release/regrab, G2 mode/owner tags, G3 consumer "
        "delivery probe, G4 real-PID IPJ watchdog")
    log("LOGGER: v3.8 todo-52e desktop-detach visibility active — mode= tag "
        "from LIVE Steam focus AppID + running-game procs (transport only as "
        "fallback, F1); startup blind window closed ([STARTUP-WINDOW] "
        "replay/synthesis of pre-loop physical churn, F2); fresh Steam poll "
        "forced at every [DETACH]/[DOCK] (F3); game=yes = active Steam-Input "
        "window, not lingering ESO/Proton procs (F4)")
    log("LOGGER: v3.9 todo-52f clear-naming + real-sleep active — human labels "
        "(controller / steam-deck / steam-deck-old / steam-controller), mode "
        "words gaming-mode|desktop, steady-state wording, controller=/virtual= "
        "snapshot fields; [SLEEP-ENTER]/[SLEEP-WAKE] only on journal-confirmed "
        "suspend/resume (no false wakes on main-loop stalls)")
    log("LOGGER: v3.10 todo-52l sleep-in-game visibility active — D1 root fix "
        "(non-blocking stdout mirror: a full journald pipe drops lines instead "
        "of freezing the whole process), D2 dense post-wake burst thread "
        "([WAKE-BURST] @0.1s for 30-60s, independent of the cadence loop), D3 "
        "[GYRO-FEED] deck/physical/center verdicts at ~2s/~10s after wake, D4 "
        "[REGRAB-MISSING] watchdog after dock, D5 [IPJ-WAKE] wake-ordered flush "
        "(real journal ts + arrival delay), D6 post-wake [CEN-IMU]/[GYRO-SRC] "
        "@1s + [RE-ENUM] on interface change, D7 [LOOP-STALL] watchdog thread")
    log("LOGGER: v3.11 todo-52o REAL input-delivery tracking active — the game "
        "reads a SEPARATE virtual pad (Microsoft X-Box 360 pad, 28de:11ff) that "
        "Steam creates, NOT the deck (28de:12f0). Deck frames = [DECK→STEAM] "
        "hop-1 only (precondition, never proof). [PAD→GAME] = pad EV events + "
        "a game pid holds the pad fd (the ONLY host-side proof). "
        "[PAD-DEAD]/[PAD-RECREATED]/[GAME-LOST-PAD] lifecycle (Valve "
        "steam-for-linux#13029). [GYRO→PAD-AXIS] when gyro lands on pad "
        "ABS_RX/ABS_RY vs [GYRO-OUTPUT] when bound to mouse/unmapped (not "
        "provable through the pad). SNAPSHOT adds deck_rd/s, pad_ev/s, "
        "game_reader, gyro_map")
    log("LOGGER: v3.12 todo-52r TRUTHFUL logger active — FIX-A real session "
        "mode ([MODE desktop|gaming|unknown via <evidence>] from ipgyro.mode: a "
        "DESKTOP session present vs a no-desktop Big Picture/gamescope GAMING "
        "session; a running game NEVER switches mode — no more gaming-mode-from-"
        "transport); FIX-B authoritative sleep (journal follower + second "
        "independent kernel-PM signal; [SLEEP-ENTER]/[SLEEP-WAKE] with wall + "
        "duration; SNAPSHOT sleep=entering|asleep|awake+<dur>); FIX-C per-data-"
        "type delivery (buttons pad_key/s + sticks pad_abs/s with SEPARATE "
        "latches vs gyro pad_gyro/s; per-source gyro carrier center|right-handle; "
        "[GYRO-REGAINED]/[GYRO-STALL-AFTER-WAKE] at the wake boundary, never "
        "detach); REAL game pid (game_reader=<pid>:<name> via SteamAppId + "
        "wine/proton tree); python-evdev backbone for the pad hop (evreader.py)")

    # (v3.8 F2) close the startup blind window ONCE, before the cadence loop:
    # replay the physical-source churn the udev thread recorded during init and
    # synthesize any [DETACH]/[DOCK] the pre-loop gap would have silently lost.
    try:
        startup_reconcile(time.monotonic())
    except Exception as e:
        log(f"LOGGER: startup reconcile error: {e}")

    last_snap = last_hb = last_iio = last_sess = last_iio_rescan = \
        last_flow = last_steam = time.monotonic()
    last_steam_state = time.monotonic()   # periodic [STEAM-STATE] cadence
    last_gyro_src = time.monotonic()      # periodic [GYRO-SRC] cadence
    last_dev_inv = time.monotonic()       # periodic [DEV-INV] re-assert
    last_cen_imu = time.monotonic()       # periodic [CEN-IMU] re-assert
    last_mode = time.monotonic()          # periodic real-mode re-scan (v3.12)

    try:
        while not S.STOP:
            now = time.monotonic()

            # -- (c) internal size rotation (cheap fstat each pass) ----------
            try:
                maybe_rotate()
            except Exception as e:
                log(f"LOGGER: rotate error: {e}")
            # -- (PM) sleep-wake detection by monotonic time gap -------------
            try:
                pm_wake_check(now)
            except Exception as e:
                log(f"LOGGER: pm wake check error: {e}")

            # -- periodic: input device snapshot + evdev re-scan ------------
            if now - last_snap >= C.SNAP_INTERVAL:
                last_snap = now
                try:
                    devices = parse_proc_input_devices()
                    new_id = device_set_id(devices)
                    if new_id != set_id:
                        set_id = new_id
                        log_snapshot(devices, full=True)
                        # (R4) full inventory on every attach/detach (device set
                        # changed) so a full-context snapshot exists, not a delta
                        log_device_inventory(devices, "attach")
                    scan_evdev(devices, build_node_map(devices))
                    found_hid = scan_hidraw()
                    state_tracker(devices, found_hid)
                except Exception as e:
                    log(f"LOGGER: snapshot/scan error: {e}")

            # -- periodic: compact heartbeat when nothing changed ------------
            if now - last_hb >= C.HB_INTERVAL:
                last_hb = now
                try:
                    log_snapshot(parse_proc_input_devices(), full=False)
                except Exception as e:
                    log(f"LOGGER: heartbeat error: {e}")

            # -- periodic: iio gyro/accel sampling ---------------------------
            # v3.2: adaptive cadence — fast (0.1 s) while a motion blip keeps
            # IIO_FAST_UNTIL armed, quiet 1/s otherwise (default preserved).
            # (e) full capture (game session / transition window) forces the
            # 0.1s IIO cadence so the IMU is sampled densely around transitions
            iio_iv = (C.IIO_FAST_INTERVAL
                      if (now < S.IIO_FAST_UNTIL or _full_mode())
                      else C.IIO_INTERVAL)
            if now - last_iio >= iio_iv:
                last_iio = now
                try:
                    sample_iio(now)
                except Exception as e:
                    log(f"LOGGER: iio sampling error: {e}")

            # -- periodic: rediscover iio (IMU can come/go with mode switches) --
            if now - last_iio_rescan >= C.IIO_RESCAN_INTERVAL:
                last_iio_rescan = now
                try:
                    discover_iio()
                except Exception as e:
                    log(f"LOGGER: iio rediscover error: {e}")

            # -- periodic: session detection ----------------------------------
            if now - last_sess >= C.SESS_INTERVAL:
                last_sess = now
                try:
                    session_scan()
                    pm_probe()   # (PM) throttled systemd sleep journal probe
                    pm_kernel_poll(now)  # (v3.12 FIX-B) kernel-PM backstop
                except Exception as e:
                    log(f"LOGGER: session scan error: {e}")

            # -- periodic: REAL session-mode re-scan (v3.12 FIX-A) ------------
            if now - last_mode >= C.MODE_SCAN_INTERVAL:
                last_mode = now
                try:
                    mode_scan(now)
                except Exception as e:
                    log(f"LOGGER: mode scan error: {e}")

            # -- periodic: per-second hidraw liveness + FLOW STOP/RESUME -----
            if now - last_flow >= C.HID_FLOW_INTERVAL:
                last_flow = now
                try:
                    emit_hid_summary(now)
                except Exception as e:
                    log(f"LOGGER: hid flow summary error: {e}")
                # v3.11 — per-second REAL-delivery verdict ([DECK→STEAM]/
                # [PAD→GAME]/[PAD-DEAD]/[GYRO→PAD-AXIS]/[GYRO-OUTPUT]). Runs on
                # the same 1 s cadence as emit_hid_summary (which just reset the
                # deck counters) so the pad/gyro counters are fresh.
                try:
                    pad_verdict_scan(now)
                except Exception as e:
                    log(f"LOGGER: pad verdict scan error: {e}")

            # -- periodic: Steam virtual-gamepad registry + controller log ----
            if now - last_steam >= C.STEAM_SCAN_INTERVAL:
                last_steam = now
                try:
                    steam_scan(now)
                except Exception as e:
                    log(f"LOGGER: steam scan error: {e}")
            # -- (Слой 2) periodic [STEAM-STATE] verdict ---------------------
            if now - last_steam_state >= C.STEAM_STATE_INTERVAL:
                last_steam_state = now
                try:
                    steam_state_verdict()
                except Exception as e:
                    log(f"LOGGER: steam state verdict error: {e}")

            # -- (R9) periodic [GYRO-SRC] source-switch verdict ---------------
            if now - last_gyro_src >= C.GYRO_SRC_INTERVAL:
                last_gyro_src = now
                try:
                    gyro_src_scan(now)
                except Exception as e:
                    log(f"LOGGER: gyro source scan error: {e}")

            # -- (R4) periodic [DEV-INV] re-assert (change-only) -------------
            if now - last_dev_inv >= C.DEV_INV_HB:
                last_dev_inv = now
                try:
                    log_device_inventory(parse_proc_input_devices(), "periodic")
                except Exception as e:
                    log(f"LOGGER: device inventory error: {e}")

            # -- (R1) periodic [CEN-IMU] re-assert (change-only) -------------
            if now - last_cen_imu >= C.IIO_RESCAN_INTERVAL:
                last_cen_imu = now
                try:
                    center_imu_scan(now)
                except Exception as e:
                    log(f"LOGGER: center imu scan error: {e}")

            # -- evdev + hidraw event capture (select drives loop cadence) ----
            # (v3.12 PART 2) drain the python-evdev pad reader's bounded queue
            # into the per-data-type counters (non-blocking; drop-oldest under
            # pressure). No-op when python-evdev is not the active reader.
            try:
                drain_events(time.monotonic())
            except Exception as e:
                log(f"LOGGER: evdev drain error: {e}")
            try:
                handle_evdev(time.monotonic())
            except Exception as e:
                log(f"LOGGER: evdev loop error: {e}")
            try:
                handle_hidraw(time.monotonic())
            except Exception as e:
                log(f"LOGGER: hidraw loop error: {e}")
    finally:
        for node, entry in S.HID_DEVICES.items():
            try:
                os.close(entry["fd"])
            except Exception:
                pass
        S.HID_DEVICES.clear()
        for path, entry in S.EV_DEVICES.items():
            try:
                os.close(entry["fd"])
            except Exception:
                pass
        S.EV_DEVICES.clear()
        if LC.LOGFILE is not None:
            try:
                LC.LOGFILE.flush()
                LC.LOGFILE.close()
            except Exception:
                pass
        log("LOGGER: shutdown complete")
