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

# v3.9 (todo 52f FIX-2 real sleep): the pm journal follower sets SLEEP_WAKE_TS
# the instant it logs a journal-confirmed [SLEEP-WAKE]; the MAIN thread applies
# the wake consequences (snapshot + full capture) exactly once via
# pm_wake_check() reading SLEEP_WAKE_TS vs WAKE_HANDLED_TS. This keeps pm.py a
# pure marker/state notifier (it cannot import obs) while the orchestrator
# (obs) still owns snapshot/full-capture, and prevents a false [SLEEP-WAKE]
# from a mere main-loop stall (stalls never set SLEEP_PENDING).
SLEEP_WAKE_TS = None        # monotonic of the last journal-confirmed wake
WAKE_HANDLED_TS = 0.0       # last wake consequences applied (monotonic)

# ---------------------------------------------------------------------------
# v3.12 (todo 52r FIX-B) — AUTHORITATIVE SLEEP bookkeeping. Every real suspend
# emits [SLEEP-ENTER]/[SLEEP-WAKE] with wall timestamps + sleep duration, and
# the SNAPSHOT sleep= field states entering|asleep|awake(+last duration). The
# enter anchor is stamped the INSTANT the first authoritative signal (journal
# follower: systemd-suspend unit and/or kernel PM lines) fires, i.e. reliably
# BEFORE the process freezes; the duration = wake_mono - enter_mono.
# ---------------------------------------------------------------------------
SLEEP_STATE = "awake"       # 'awake' | 'entering' | 'asleep'
SLEEP_ENTER_MONO = None     # monotonic of the last journal-confirmed enter
SLEEP_ENTER_WALL = ""       # wall "HH:MM:SS" of the last confirmed enter
SLEEP_ENTER_LOG = 0.0       # monotonic when the [SLEEP-ENTER] line was logged
SLEEP_LAST_DUR = None       # last sleep duration seconds (float) or None
SLEEP_WAKE_WALL = ""        # wall "HH:MM:SS" of the last confirmed wake
WAKE_GYRO_COMPARED = 0.0    # monotonic of the last post-wake gyro compare
WAKE_GYRO_STALLED = False   # True once the current wake logged a gyro stall

# (R2) full-mode per-frame capture: last per-frame log time per hidraw node
# (anti-spam floor so a pathological 240 frame/s deck cannot flood forever).
FRAME_LOG_LAST = {}         # hidraw node -> monotonic last [FRAME] log time

# ---------------------------------------------------------------------------
# v3.8 — todo 52e desktop-detach visibility (F1-F4).
# ---------------------------------------------------------------------------
# (F1/F4) parsed Steam focus + the LIVE mode verdict shared with obs/capture.
# STEAM_FOCUS_APPID/KIND are derived from STEAM_UI_FOCUS; MODE_CTX is the
# 'game'|'desktop' verdict computed from the active Steam-Input window (focused
# AppID) with the running-game proc set as fallback when focus is unknown.
STEAM_FOCUS_APPID = None     # focused AppID parsed from STEAM_UI_FOCUS
STEAM_FOCUS_KIND = None      # 'GAME'|'769'|'413080'|'OTHER'|None
MODE_CTX = None              # live mode verdict: 'game'|'desktop'|None
MODE_CTX_DETAIL = ""         # human reason for the last MODE_CTX
GAME_FOCUSED = False         # F4: the active Steam-Input window IS a game

# (F2) startup blind window: physical-source churn recorded by the udev thread
# between process start and the first main-loop scan (the startup scan already
# set PHYS_STATE as the baseline). Replayed ONCE by obs.startup_reconcile() so
# a detach+re-dock fully inside the init gap is never silently lost.
STARTUP_CHURN = []           # [{"kind": "detach"|"dock", "ts": monotonic}]
STARTUP_BASELINE_TS = None   # monotonic of the first PHYS_STATE baseline scan
TRACK_LIVE = False           # True once obs.startup_reconcile() has run

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
# v3.12 (todo 52r) — REAL session-mode verdict (FIX-A). The v3.11 mode= was
# derived from the TRANSPORT deck-output roles / the Steam-Input focused window,
# which reported gaming-mode while the user sat in a DESKTOP session with ESO
# running. mode.py now classifies the REAL Steam UI/session mode (a DESKTOP
# session present vs a no-desktop Big Picture/gamescope GAMING session). A
# running game NEVER changes this verdict. Read/written as S.<name>.
# ---------------------------------------------------------------------------
MODE = None              # 'desktop' | 'gaming' | 'unknown' (last verdict)
MODE_SRC = []            # evidence strings behind the verdict ([MODE ... via])
MODE_DETAIL = ""         # human reason for the verdict (unknown reasons etc.)
MODE_LAST_LOG = 0.0      # monotonic last [MODE] change log (anti-spam)

# ---------------------------------------------------------------------------
# v3.10 (todo 52l) — sleep-in-game visibility (D1-D7) shared state.
# ---------------------------------------------------------------------------
# D2/D3/D6 — wake-burst thread lifecycle. The thread is spawned by pm._pm_emit
# (the journal follower, a daemon thread) the instant a real [SLEEP-WAKE] is
# journal-confirmed, and by obs.pm_wake_check (mono-gap backstop) as a fallback.
# It runs dense 0.1s deck/physical/IIO sampling INDEPENDENT of the cadence loop,
# so a stall in the loop can never stop the post-wake capture.
WAKE_BURST_ACTIVE = False       # True while a wake-burst thread is running
WAKE_BURST_UNTIL = 0.0          # monotonic end of the current burst window
WAKE_BURST_START = 0.0          # monotonic start of the current burst
# D4 — re-grab watchdog latch (the dock anchor is DOCK_AT above; the latch
# ensures [REGRAB-MISSING] fires once per dock window while a late real
# [STEAM-REGRAB]/[DECK-ALIVE] still measures [REGRAB-DT]).
REGRAB_MISSING_LOGGED = False   # True once the current dock window logged [REGRAB-MISSING]
# D6 — [RE-ENUM] identity diff across a detach/dock. capture.scan_hidraw keeps
# S.PHYS_ID_LAST fresh while the physical source is present; at [DETACH] it is
# captured into S.PHYS_ID_BEFORE_DETACH so a dock that returns as a DIFFERENT
# interface (e.g. controller@1.1 -> controller@1.3, IMU ts reset, state
# (attached,detached)) is logged as [RE-ENUM] — device identity changed.
PHYS_ID_LAST = None             # last healthy physical-source identity tuple
PHYS_ID_BEFORE_DETACH = None    # physical identity at the last [DETACH]
# D7 — loop-stall watchdog latches (the cadence loop keeps S.LAST_TICK fresh via
# pm_wake_check; the watchdog thread reports a >LOOP_STALL_LOG_S silence).
LOOP_STALL_ACTIVE = False       # True while a >LOOP_STALL_LOG_S silence is on
LOOP_STALL_LAST_LOG = 0.0       # monotonic last [LOOP-STALL]/[LOOP-RECOVERED] log
# D5 — ipj wake flush dedupe (one short journal scan at the wake instant).
IPJ_WAKE_FLUSHED = 0.0          # monotonic last ipj wake flush

# ---------------------------------------------------------------------------
# v3.11 (todo 52o) — REAL input-delivery tracking shared state. The game reads
# a SEPARATE virtual pad node Steam creates ("Microsoft X-Box 360 pad",
# VID:PID 28de:11ff) — deck frames (28de:12f0 -> Steam) are only HOP 1, never
# proof the game got the input. The ONLY host-side proof is EV events on the
# pad node WHILE the game process holds the pad fd. All state is read/written
# as S.<name> (never rebound locally).
#
# v3.12 (todo 52r FIX-C) — BUTTONS and GYRO are modelled as INDEPENDENT
# per-source streams with SEPARATE counters + anti-spam latches, so one stall
# can never mask the other and the log can honestly show "buttons reached the
# game, gyro did not" after a wake. The v3.11 SHARED [PAD→GAME] latch
# (PAD_DELIVERY_LAST) starved the per-type verdict — split into per-data-type
# delivery latches below (PAD_KEY_LATCH / PAD_STICK_LATCH / gyro axis latch).
# ---------------------------------------------------------------------------
# Pad identity (populated by devices.scan_evdev from /proc/bus/input/devices).
# The pad is matched by NAME PREFIX + VID:PID 28de:11ff (NOT the churning
# eventN, which changes on every Steam teardown/recreate).
PAD_NODE = None             # '/dev/input/eventN' of the 28de:11ff pad (or None)
PAD_SYS = None              # sysfs path of the pad input (e.g. .../input65)
PAD_NAME = None             # full pad name (e.g. 'Microsoft X-Box 360 pad 0')
PAD_LAST_EV = 0.0           # monotonic of the last EV event observed on the pad
PAD_EV_COUNT = 0            # EV events on the pad since the last per-second pass
PAD_ABS_COUNT = 0           # EV_ABS events (any axis) on the pad since the pass
PAD_GYRO_AXIS_COUNT = 0     # gyro-axis (ABS_RX/ABS_RY) events on the pad
PAD_GYRO_LAST = 0.0         # monotonic of the last gyro-axis pad event
# v3.12 — per-DATA-TYPE pad counters (independent; the v3.11 shared-latch
# starvation is why [PAD→GAME] could show 0 during proven delivery).
PAD_KEY_COUNT = 0           # EV_KEY (button) events on the pad since the pass
PAD_KEY_LAST = 0.0          # monotonic of the last pad EV_KEY event
PAD_STICK_COUNT = 0         # non-gyro EV_ABS (sticks/triggers) on the pad
PAD_STICK_LAST = 0.0        # monotonic of the last pad non-gyro EV_ABS event
# per-DATA-TYPE anti-spam delivery latches (independent per stream).
PAD_KEY_LATCH = 0.0         # last [PAD→GAME buttons pad_key/s] log
PAD_STICK_LATCH = 0.0       # last [PAD→GAME sticks pad_abs/s] log
PAD_DEAD_LOGGED = False     # True while a [PAD-DEAD] window is open (re-assert)
PAD_DEAD_LAST = 0.0         # monotonic last [PAD-DEAD] log (anti-spam)
PAD_RECREATED_LAST = 0.0    # monotonic last [PAD-RECREATED] log (anti-spam)
PAD_GYRO_AXIS_LOGGED = False  # True while a [GYRO→PAD-AXIS] window is open
PAD_GYRO_AXIS_LAST = 0.0    # monotonic last [GYRO→PAD-AXIS] log (anti-spam)
PAD_DELIVERY_LAST = 0.0     # monotonic last [PAD→GAME] delivery log (anti-spam)
PAD_GAME_LOST_LAST = 0.0    # monotonic last [GAME-LOST-PAD] log (anti-spam)
PAD_READER_PIDS = set()     # pids holding the pad fd (game reader probe)
PAD_READER_KNOWN = None     # last reader-pid set signature (change-only)
# Per-second deck (hop-1) counters, reset by the obs pad_verdict_scan pass.
DECK_RD_COUNT = 0           # deck frames read since the last per-second pass
DECK_MOTION_COUNT = 0       # deck IMU-motion frames since the last pass
DECK_ACTION_COUNT = 0       # deck button/stick/trigger changes since the pass
# Last [PAD→GAME] evidence (for the SNAPSHOT game_reader= field + markers).
PAD_GAME_READER = False     # True when a game pid holds the pad fd
PAD_GAME_READER_PID = None  # the game pid holding the pad fd (or None)
PAD_GAME_READER_NAME = ""   # human name of the real game reader (e.g. eso64.exe)
PAD_GAME_READER_APPID = None  # SteamAppId of the real game reader (e.g. 306130)
PAD_GYRO_MAP = None         # 'pad-axes' | 'mouse' | 'none' | None (unknown)
# v3.12 — WAKE-vs-GYRO attribution state. Pre-sleep gyro/button "was flowing"
# baselines are captured at [SLEEP-ENTER] (before the freeze) so the post-wake
# comparison can honestly say "gyro reached the pad before sleep, and does/does
# not after wake while buttons keep flowing". gyro_is_live is a per-source
# boolean re-derived by gyro.py each window.
PRE_SLEEP_BTN = None        # None|bool: pad buttons flowing just before sleep
PRE_SLEEP_GYRO = None       # None|bool: pad gyro axes flowing just before sleep
GYRO_IS_LIVE = None         # per-source verdict: the physical gyro live now
GYRO_SRC_CARRIER = None     # which physical gyro carries it (center/right-handle)
# python-evdev reader liveness (evreader.py owns the pad node when active).
EVREADER_ON = False         # True while the evdev pad reader thread is active
EVREADER_ERR = ""           # last python-evdev reader error ('' = clean)
EVREADER_STARTED = False    # True once start_evreader() ran (even if evdev off)

# ---------------------------------------------------------------------------
# Shutdown flag (set by logcore._signal_handler, read by every daemon loop)
# ---------------------------------------------------------------------------
STOP = False                # set by SIGINT/SIGTERM for a clean exit
