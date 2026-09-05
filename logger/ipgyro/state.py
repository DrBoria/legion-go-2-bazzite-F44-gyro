# -*- coding: utf-8 -*-
"""
ipgyro.state — THE single home for every shared MUTABLE global of the logger.

Why this module exists (and the rule the whole split obeys)
-----------------------------------------------------------
Python's `global X` only ever refers to the CURRENT module's namespace, so
`from .state import X` + a later `X = ...` in another module would silently
create a MODULE-LOCAL and break shared state. Therefore no module ever
rebinds one of these names locally: every consumer does
`from . import state as S` and reads/writes shared mutable state ONLY as
`S.NAME` attributes. Dicts/lists stored here are mutated in place through
that same attribute, so all modules always see the same live objects.

Two boot anchors are also computed here (once, at import):
  BOOT_ID  = first 8 chars of the kernel boot_id, appended to every log line
  BOOT_T0  = monotonic origin, so log() can report [boot=.. mono=..]

The log-file handle + its lock deliberately do NOT live here: they are the
"log core" and stay private to ipgyro.logcore (LOGFILE / LOG_LOCK), which is
the only place that opens/flushes/rotates them (app.main() opens it through
`logcore.LOGFILE = ...`). Every OTHER piece of mutable process state is here.
"""

import time

from . import conf as C

# ---------------------------------------------------------------------------
# (a) boot anchor carried on every log line
# ---------------------------------------------------------------------------
BOOT_ID = C._read_boot_id()
BOOT_T0 = time.monotonic()

# ---------------------------------------------------------------------------
# Session tracking
# ---------------------------------------------------------------------------
SESSION_SET = {}
GAMESCOPE_PID = None

# ---------------------------------------------------------------------------
# evdev capture state: path -> {"fd", "name", "buf", "last_abs"}
# ---------------------------------------------------------------------------
EV_DEVICES = {}
EV_OPEN_ERR = {}

# ---------------------------------------------------------------------------
# iio throttle state + discovered device list
# ---------------------------------------------------------------------------
IIO_LAST_ACTIVE = {}
IIO_LAST_IDLE = {}
IIO_DEVICES = []
# v3.2 — deadline until which the fast (0.1 s) IIO sampling window stays armed
IIO_FAST_UNTIL = 0.0        # monotonic; extended by every gyro blip

# ---------------------------------------------------------------------------
# v2 — hidraw / Steam / InputPlumber journal state
# ---------------------------------------------------------------------------
HID_DEVICES = {}            # hidraw node -> {fd,path,label,vid,pid,iface,...}
HID_OPEN_ERR = {}           # /dev/hidrawN -> last open-error time (anti-spam)
STEAM_CACHE = {}            # steam file path -> {reg_sig / ctl_pos / ...}
IPJ_FLOOD = [0.0, 0]        # InputPlumber journal flood gate [window, lines]
LAST_STATE = None           # last computed mode/attach signature (for diffs)

# v3.4 — aggregate physical (17EF) dock state for [DETACH]/[DOCK];
# None = unknown (first scan) | True docked | False detached
PHYS_STATE = None

# v3.1 — Steam Input activation on the running game (direction D)
STEAM_UI_FOCUS = None       # last focused AppID label logged (change-only)
STEAM_APP_SUMMARY = None    # last "running Steam games" summary (change-only)
STEAM_PROC_SET = {}         # pid -> (appid, name, cmd) of running Steam games

# ---------------------------------------------------------------------------
# v3 — physical-input activity signal (fed by _process_event; reset in the
# per-second HIDFLOW pass). Drives the FLOW GAP "deck silent while active"
# marker so the loss point is visible even without decoding the source proto.
# ---------------------------------------------------------------------------
EV_ACT_COUNT = 0            # EV KEY/ABS events since the last ACTIVITY line
EV_ACT_LAST = 0.0           # monotonic time of the most recent EV event

# ---------------------------------------------------------------------------
# v3.5 — (d/e) full-capture window + PM/Steam-layer state
# ---------------------------------------------------------------------------
FULL_RECORD_UNTIL = 0.0     # monotonic deadline of the full-mode window
GAME_RECORD = False         # True while a Steam game is running
LAST_FULL_LOG = 0.0         # anti-spam for the [FULL-ON] marker
DECK_KEEPALIVE = None       # last IPJ "keeping target ... deck-uhid" line
SLEEP_PENDING = False       # PM: between [SLEEP-ENTER] and [SLEEP-WAKE]
LAST_PM_PROBE = 0.0         # pm_probe() journal throttle (monotonic)
LAST_TICK = 0.0             # main-loop wake-gap detector (monotonic)
DETACH_AT = None            # monotonic time of the last [DETACH] (dt measure)

# ---------------------------------------------------------------------------
# v3.6 — todo 51d "ЛОГ ВСЁ" audit: shared state for the new markers.
# ---------------------------------------------------------------------------
# (R1) identity of the ATTACHED CENTER body IMU (the iio device InputPlumber
# binds as the center / right_gyro slot source, e.g. iio:device2 gyro_3d).
CEN_IMU = None              # dict {entry,name,dpath,slot} or None if unknown
CEN_IMU_LAST = 0.0          # anti-spam for the [CEN-IMU] marker (monotonic)

# (R9) active gyro source verdict (which source the gyro stream is on + why).
GYRO_SRC = None             # last [GYRO-SRC] verdict string (change-only)
GYRO_SRC_LAST = 0.0         # last [GYRO-SRC] scan time (monotonic)
GYRO_SRC_SEEN = None        # last mirrored [GYRO-SRC] journal line (dedupe)

# (R8) dock time anchor for dock -> game-binds-again ([REGRAB-DT]) latency.
DOCK_AT = None              # monotonic time of the last [DOCK] (regrab-dt)
REGRAB_DT_LOGGED = False    # True once the current dock window's regrab-dt logged

# (R4) last full device-inventory signature (change-only re-assert).
DEV_INV_SIG = None          # md5 of the last [DEV-INV] inventory (change-only)

# (R6) PM journal follower state (reliable [SLEEP-ENTER]/[SLEEP-WAKE]).
PM_FOLLOW_LAST = 0.0        # last [PM-LIVE] heartbeat (monotonic)

# (R2) full-mode per-frame capture: last per-frame log time per hidraw node
# (anti-spam floor so a pathological 240 frame/s deck cannot flood forever).
FRAME_LOG_LAST = {}         # hidraw node -> monotonic last [FRAME] log time

# ---------------------------------------------------------------------------
# Runtime config (todo 52a/52b) — set ONCE by app.main() from ipgyro.config.
# S.CFG is the effective Cfg (tomllib /etc/ip-gyro-logger/config.toml merged
# over strict defaults); S.LABELS is the resolved [labels] role->display dict.
# Modules read these lazily (never None) so the logger runs with defaults even
# before app.main() loads the file.
# ---------------------------------------------------------------------------
CFG = None                  # ipgyro.config.Cfg (set by app.main(); None = defaults)
LABELS = None               # [labels] dict (resolved once by app.main())

# ---------------------------------------------------------------------------
# Shutdown flag (set by logcore._signal_handler, read by every daemon loop)
# ---------------------------------------------------------------------------
STOP = False                # set by SIGINT/SIGTERM for a clean exit
