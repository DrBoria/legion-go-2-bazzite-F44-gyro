# -*- coding: utf-8 -*-
"""
ipgyro.conf — compile-time constants, regexes and the boot-id reader. Pure data
/ pure functions: no shared MUTABLE state lives here, so every consumer imports
names by value safely (constants are never rebound at runtime). Importing this
module has no side effects beyond compiling the regexes once. Split from the
monolithic ip-gyro-logger.py on 2026-09-05 (todo 51c) with EXACTLY preserved
values/behaviour.

Device-id policy (todo 52a/52b): this module holds NO vendor/product ids and NO
decode tables. Protocol decode tables + frame-header signatures live in
ipgyro.codecs (bound by capability, not pid); runtime device identity is
discovered in ipgyro.discover; tunables live in the config (ipgyro.config).
"""

import re
import struct

PROG = "ip-gyro-logger"

# ---------------------------------------------------------------------------
# evdev event codes + native input_event struct size
# ---------------------------------------------------------------------------

EV_KEY = 0x01
EV_ABS = 0x03

# struct input_event, native layout: 2x signed long long (sec, usec),
# unsigned short (type), unsigned short (code), signed int (value) -> 24 bytes.
EVENT_SIZE = struct.calcsize("<2qHHi")

# Human-readable names for the gamepad codes we care about. Unknown codes are
# printed as 0xNNN / 0xNN so logs stay greppable and complete.
KEY_NAMES = {
    0x105: "KEY_LEFT", 0x106: "KEY_RIGHT", 0x107: "KEY_UP", 0x108: "KEY_DOWN",
    0x11c: "KEY_ENTER",
    0x120: "BTN_TRIGGER", 0x121: "BTN_THUMB", 0x122: "BTN_THUMB2",
    0x128: "BTN_TOP", 0x129: "BTN_TOP2", 0x12a: "BTN_PINKIE", 0x12b: "BTN_BASE",
    0x130: "BTN_SOUTH", 0x131: "BTN_EAST", 0x132: "BTN_NORTH", 0x133: "BTN_WEST",
    0x134: "BTN_TL", 0x135: "BTN_TR", 0x136: "BTN_TL2", 0x137: "BTN_TR2",
    0x138: "BTN_SELECT", 0x139: "BTN_START", 0x13a: "BTN_MODE",
    0x13b: "BTN_THUMBL", 0x13c: "BTN_THUMBR",
    0x220: "BTN_DPAD_UP", 0x221: "BTN_DPAD_DOWN", 0x222: "BTN_DPAD_LEFT",
    0x223: "BTN_DPAD_RIGHT",
    0x2c0: "BTN_DPAD_UP_2", 0x2c1: "BTN_DPAD_DOWN_2",
    0x2c2: "BTN_DPAD_LEFT_2", 0x2c3: "BTN_DPAD_RIGHT_2",
}
ABS_NAMES = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z",
    0x03: "ABS_RX", 0x04: "ABS_RY", 0x05: "ABS_RZ",
    0x06: "ABS_THROTTLE", 0x07: "ABS_RUDDER", 0x08: "ABS_WHEEL",
    0x09: "ABS_GAS", 0x0a: "ABS_BRAKE",
    0x10: "ABS_HAT0X", 0x11: "ABS_HAT0Y",
    0x18: "ABS_HAT1X", 0x19: "ABS_HAT1Y",
}

# Common gamepad button/axis bit numbers (linux/input-event-codes.h).
GAMEPAD_KEY_BITS = (
    0x130, 0x131, 0x132, 0x133, 0x134, 0x135, 0x136, 0x137,
    0x138, 0x139, 0x13a, 0x13b, 0x13c, 0x120, 0x129, 0x12a,
    0x220, 0x221, 0x222, 0x223,
)
GAMEPAD_ABS_BITS = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11)

# Generic gamepad name hints (capability fallback only — no product ids).
NAME_GAMEPAD_HINTS = (
    "xbox", "controller", "gamepad", "joystick",
)

# ---------------------------------------------------------------------------
# Throttle intervals (seconds).
# ---------------------------------------------------------------------------
SNAP_INTERVAL = 3.0         # re-read /proc/bus/input/devices
HB_INTERVAL = 15.0          # compact heartbeat when the device set did not change
IIO_INTERVAL = 1.0          # sample gyro/accel
IIO_IDLE_HB = 5.0           # one idle heartbeat per device when magnitude == 0
# v3.2 — fast IIO sampling while the IMU is actually moving (burst capture).
# A first gyro blip arms a fast window; each new blip re-arms it. The quiet
# default behaviour stays at 1 sample/s.
IIO_FAST_INTERVAL = 0.1     # fast cadence while motion is present (10 Hz)
IIO_FAST_HOLD = 2.0         # stay fast this long after the last motion blip
SESS_INTERVAL = 5.0         # loginctl session scan
IIO_RESCAN_INTERVAL = 60.0  # rediscover iio devices (IMU can come/go)
EV_THROTTLE = 0.1           # ~10 EV_ABS lines/sec/device
EV_OPEN_ERR_HB = 60.0       # don't spam "cannot open /dev/input/eventX" errors

IIO_DIR = "/sys/bus/iio/devices"
IIO_RAW_GYRO_RE = re.compile(r"^in_(anglvel|gyro)(_[xyz])?_raw$")
IIO_RAW_ACCEL_RE = re.compile(r"^in_accel(_[xyz])?_raw$")

# v2 — new capture channels and their throttle cadences
HID_FLOW_INTERVAL = 1.0     # per-device hidraw liveness summary (once/sec)
HID_RAW_SAMPLE = 5.0        # full-report hex sample cadence per hidraw
FLOW_STOP_AFTER = 2.0       # silence after data -> log FLOW STOP
FLOW_NEVER_AFTER = 10.0     # attached but never delivered -> FLOW STOP
FLOW_STOP_HB = 20.0         # re-assert a long-standing FLOW STOP only this often
STEAM_SCAN_INTERVAL = 5.0   # Steam registry / controller-log diff cadence
STEAM_TAIL_BYTES = 262144   # max controller.txt bytes considered per scan
IPJ_BACKLOG_LINES = 150     # journalctl -u inputplumber -n backlog at startup
IPJ_MAX_LINES_S = 25        # InputPlumber journal flood gate (lines/sec)
# InputPlumber journal relevance — architectural vocabulary only (no device ids).
IPJ_RE = re.compile(
    r"(attach|detach|disconnect|connect|composite|source|target|grab|release|"
    r"hidraw|gyro|calib|CEN-|deck|uhid|vhci|"
    r"error|fail|panic|warn|exception|open|close|spawn|kill|session)",
    re.IGNORECASE,
)
# Steam controller-log relevance — architectural vocabulary only (no device ids).
CTL_RE = re.compile(
    r"(steam deck|steam controller|imu|gyro|haptic|"
    r"register|error|fail)", re.IGNORECASE)

# v3.1 — Steam Input activation evidence (direction D). Steam's per-app
# controller_ui.txt tells us WHICH app Steam Input is configured for (per-game
# "Loaded Config ... App ID N", the focused game/client window AppID, controller
# connect + ProductID/Serial), i.e. whether Steam Input really activated on the
# launched game. Running game processes (SteamAppId in /proc environ) close the
# loop. Captured tail-only, heavily filtered, capped per scan.
STEAM_UI_TAIL_BYTES = 262144
STEAM_UI_MAX_LINES = 8
STEAM_UI_FOCUS_RE = re.compile(
    r"OnFocusWindowChanged to (game window type|window type):?.*?\bAppID ([0-9]+)",
    re.IGNORECASE)
# coarse candidate set: per-game config loads, focus events, controller identity
STEAM_UI_CAND_RE = re.compile(
    r"(OnFocusWindowChanged|Controller [0-9]+ (connected|disconnected|attributes)|"
    r"ProductID|Serial|Custom SDL Mapping|Loaded Config)", re.IGNORECASE)
# background config loads that fire for EVERY controller / client UI / desktop
# shell constantly (chord/basicui/desktop) — the per-GAME selection is the real
# signal, so these are excluded from the raw capture.
STEAM_UI_NOISE_RE = re.compile(
    r"(Last Resort Path|basicui_neptune|chord_neptune|desktop_neptune|"
    r"App ID (769|443510|413080)|AppID (769|443510|413080))", re.IGNORECASE)

HIDRAW_DIR = "/sys/class/hidraw"

# ---------------------------------------------------------------------------
# v3 — per-direction flow correlation / gap-detection cadence
# ---------------------------------------------------------------------------
FLOW_GAP_AFTER = 2.0        # deck silent this long before a FLOW GAP is flagged
FLOW_GAP_HB = 20.0          # re-assert a long-lived FLOW GAP only this often
FLOW_EV_WINDOW = 2.0        # an EV event this recent counts as "user active"
HID_MOTION_LOG_MIN = 0.5    # at most ~2 MOTION lines/sec/deck while moving

# v3.2 — physical XInput raw capture + deck coalescing cadences (no device ids)
LEGO_RAW_HB = 5.0           # periodic full raw-hex cadence for the physical src
LEGO_MOTION_LOG_MIN = 0.1   # <= ~10 IMU DECODE lines/sec/source while moving
DECK_DRAIN_MAX = 16         # max frames coalesced from one deck burst drain

# ---------------------------------------------------------------------------
# v3.5 — V22 full-observability contract cadences + sizes.
# (Contract text formerly in MIGRATION-V22-FULL-OBSERVABILITY.md — deleted
#  2026-09-05 on user request; the full contract now lives in Agent.md,
#  section "2026-09-05 ~22:3x — V22 ДИЗАЙН-ДОКУМЕНТ ...".)
# ---------------------------------------------------------------------------
LOG_VERSION = "v3.15"

# ---------------------------------------------------------------------------
# v3.12 (todo 52r) — truthful mode/sleep/per-source delivery cadences.
# ---------------------------------------------------------------------------
# (FIX-A) real session-mode scan cadence (mode.py classify is cheap; a change
# log is anti-spammed internally). The [MODE ... via ...] change marker fires
# on change; the periodic re-scan keeps the SNAPSHOT mode= field fresh.
MODE_SCAN_INTERVAL = 5.0    # periodic real-mode re-scan (seconds)
# (FIX-B) authoritative sleep: the second independent signal (kernel PM lines
# via journalctl -k) is polled on this cadence as a backstop when the
# systemd-suspend unit follower stalls; the mono-gap backstop in obs is gated
# on S.SLEEP_PENDING as before.
PM_KERNEL_POLL = 5.0        # kernel-PM journal backstop poll cadence (seconds)
PM_KERNEL_WINDOW = 120      # journalctl -k --since window for the backstop
# (FIX-C) post-wake gyro-vs-button compare window: after a wake we compare the
# pre-sleep pad gyro/button baseline against the first WAKE_GYRO_COMPARE_S of
# post-wake pad flow and emit [GYRO-REGAINED]/[GYRO-STALL-AFTER-WAKE].
WAKE_GYRO_COMPARE_S = 8.0   # seconds after wake to judge gyro regain/stall

# ---------------------------------------------------------------------------
# v3.13 (todo 52t) — PER-HOP FLOW PROOF cadences. The logger must PROVE WHERE
# in the chain the gyro (and any data type) is lost, hop by hop, with evidence
# on every line — never «не доказуемо» when a game is running and detectable.
# H0 physical source -> H1 deck->Steam -> H2 Steam routing config (the game
# AppID vs desktop 413080) -> H3 Steam pad 28de:11ff -> the game reader.
# ---------------------------------------------------------------------------
FLOW_MIN_LOG = 5.0          # min seconds between repeated compact [FLOW] lines
FLOW_CHANGE_MIN = 2.0       # min seconds between repeated per-type verbose
                            # [*FLOW] markers on a state-flapping transition
WAKE_FLOW_S = 3.0           # per-hop flow verdict emitted ~2-5 s after wake
FOCUS_LOST_HB = 10.0        # [STEAM-FOCUS-LOST-GAME] re-assert while held
                            # (the routing proof stays loud, not a one-shot)
PAD_STALL_REQ = 5.0         # [PAD-DEAD] needs PAD_STALL_REQ CONSECUTIVE quiet
                            # seconds (accumulated), matching the old text; the
                            # v3.12 bug fired on the FIRST quiet second.

# ---------------------------------------------------------------------------
# v3.15 (todo 52z) — corrected rebuild on the v3.13 base (the over-engineered
# v3.14 was reverted). Ground-truth fixes:
#  * game_reader reports the REAL game (eso64.exe), not a wine service host
#    (svchost/bufferselfpatch) — obs._main_game_proc resolves the pad-fd
#    reader's Steam-game ancestor first.
#  * a [DETACH] at a sleep-wake boundary when the controller was ATTACHED at
#    [SLEEP-ENTER] is a SUSPEND RE-ENUMERATION artifact (the USB controller
#    powered down during sleep and re-enumerates on wake), NOT a real detach —
#    the user's ground truth: "Sleep был точно с attached". The logger must not
#    report a false detach/attach cycle around a wake.
#  * the gyro carrier is derived from the PHYSICAL ATTACH STATE (right handle
#    17ef:61ed detached = right-handle carrier), not from the always-present
#    center IIO — the user's ground truth: when he detached, the RIGHT handle
#    gyro worked (GyroR), not center.
# ---------------------------------------------------------------------------
WAKE_DETACH_GRACE_S = 8.0   # after a [SLEEP-WAKE], a physical [DETACH] within
                            # this window while the controller was attached at
                            # [SLEEP-ENTER] is treated as a suspend re-enum
                            # (suppressed as a real detach), not a user detach.
RIGHT_HANDLE_PID = "61ED"   # observable pid of the detached right handle (its
                            # own gyro source) — identity only, never a selector.
FULL_CTRL_PID = "61EB"      # observable pid of the full controller (center gyro
                            # in the XInput right_gyro slot while attached).

# v3.9 (todo 52f FIX-1 CLEAR NAMING): the HUMAN vocabulary every log/config
# shows. Internal role/deck_kind keys (discover.py), mode internals and marker
# TOKENS ([DETACH]/[DOCK]/[SLEEP-*]/...) are untouched — these tables translate
# ONLY the user-facing words, so the log reads plainly ("mode=gaming-mode",
# "controller=[...]", "steady-state") instead of internal codes ("mode=GAMING",
# "legion=[LEGION-SRC...]", "baseline"). Config [labels] carries the same
# human device names (see config.py defaults + logger/config.ipgyro.toml).
DECK_MODE_WORDS = {          # obs._deck_mode() internal -> human word
    "GAMING": "gaming-mode",
    "DESKTOP": "desktop",
    "TRANSITION-GAMING+DESKTOP": "gaming-mode + desktop (transition)",
    "NO-DECK": "no-deck",
}
MODE_TAG_WORDS = {           # obs._live_mode_tag() 'game'|'desktop'|'none'
    "game": "gaming-mode",
    "desktop": "desktop",
    "none": "none",
}

# (c) internal size rotation — never trust logrotate alone again (968MB file)
LOGGER_MAX_BYTES = 300 * 1024 * 1024

# (b) IPJ mirror watchdog cadence (seconds)
IPJ_WATCHDOG_S = 90.0        # no journal line this long -> assume mirror dead
IPJ_LIVE_HB_S = 15.0         # periodic [IPJ-LIVE] heartbeat while healthy
IPJ_RESTART_DELAY = 3.0      # pause before restarting journalctl
IPJ_SILENT_COOLDOWN = 300.0  # no repeated [IPJ-DEAD] while inputplumber idle

# (e) full-mode capture window: held this long after the last transition
FULL_MODE_POST = 10.0        # ~10s after a transition
FULL_MODE_WAKE = 30.0        # extra hold after a sleep wake
FULL_MODE_GAME = 3600.0      # effectively the whole game session

STEAM_STATE_INTERVAL = 30.0  # periodic [STEAM-STATE] cadence

# ---------------------------------------------------------------------------
# v3.6 — todo 51d "ЛОГ ВСЁ" audit: cadences + journal-window regexes for the
# new markers ([CEN-IMU]/[GYRO-SRC]/[DEV-INV]/[FRAME]/[IPJ-*]/[GAME-BIND]/
# [GAME-LOSS]/[REGRAB-DT]/[SLEEP-ENTER]/[SLEEP-WAKE] reliable follower).
# ---------------------------------------------------------------------------

# (R6) systemd-suspend journal follower (reliable pre-freeze [SLEEP-ENTER]).
PM_JOURNAL_UNIT = "systemd-suspend"
PM_TAIL_BYTES = 262144          # journalctl -n cap for the one-shot backlog
PM_FOLLOW_HB = 60.0             # periodic [PM-LIVE] heartbeat while following

# (R9) gyro-source arbiter verdict cadence + journal-window reader.
GYRO_SRC_INTERVAL = 5.0         # periodic [GYRO-SRC] verdict cadence
GYRO_JOURNAL_S = 30.0           # journalctl --since window for source-switch scan
GYRO_JOURNAL_LINES = 200        # journalctl -n cap for the source-switch scan

# (R4) full device-inventory cadence (startup + attach/detach are event-driven;
# this is the periodic re-assert so a long-lived inventory is never stale).
DEV_INV_HB = 60.0

# ---------------------------------------------------------------------------
# v3.10 (todo 52l) — sleep-in-game VISIBILITY fixes D1-D7.
# ---------------------------------------------------------------------------
# D2/D3/D6 — dense post-wake capture: after every [SLEEP-WAKE] a dedicated wake
# burst thread force-reads deck hidraw + physical XInput right-gyro + center IIO
# at WAKE_BURST_IV (0.1s) for WAKE_BURST_S seconds, INDEPENDENT of the cadence
# loop (so a 24-33s main-loop stall can never hide the wake transition again —
# the first ~16s after wake, when InputPlumber's gyro arbiter re-initialized,
# is exactly what a stalled loop used to leave UNSAMPLED).
WAKE_BURST_S = 60.0            # dense post-wake capture window (30-60s contract)
WAKE_BURST_IV = 0.1            # dense sampling cadence inside the burst (10 Hz)
GYRO_FEED_S = (2.0, 10.0)      # [GYRO-FEED] "did the feed resume" instants (~2s/~10s)
WAKE_REASSERT_S = 30.0         # post-wake [CEN-IMU]/[GYRO-SRC] re-assert window (@1s)
# D4 — re-grab watchdog: after every [DOCK], if no [STEAM-REGRAB]/[DECK-ALIVE]
# within REGRAB_MISSING_S -> [REGRAB-MISSING] (turns a silent 55-minute absence
# into a loud, dated marker).
REGRAB_MISSING_S = 15.0
# D7 — loop-stall watchdog: report when the cadence loop produces nothing for
# longer than LOOP_STALL_LOG_S ([LOOP-STALL]); re-assert only every LOOP_STALL_HB.
LOOP_STALL_LOG_S = 10.0
LOOP_STALL_HB = 15.0

# ---------------------------------------------------------------------------
# v3.11 (todo 52o) — REAL input-delivery tracking cadences. The game reads a
# SEPARATE uinput evdev node Steam creates ("Microsoft X-Box 360 pad",
# VID:PID 28de:11ff) — deck frames (28de:12f0 -> Steam) are only HOP 1, never
# proof the game got the input. The pad identity constants live in devices.py
# (this module holds NO device ids by policy); only cadences live here.
# ---------------------------------------------------------------------------
PAD_STALL_S = 5.0       # pad present + game reader + deck delivering, zero pad
                        # EV events this long -> [PAD-DEAD] (Valve #13029 mode)
PAD_DELIVERY_HB = 30.0  # re-assert a long-lived [PAD→GAME]/[GYRO→PAD-AXIS]
                        # delivery state only this often (no per-second spam)
# D5 — ipj wake flush: journal window (backward from the wake instant) scanned
# once at wake to catch delayed arbiter/gyro-center flip-flops the -f follower
# delivered late across resume.
IPJ_WAKE_FLUSH_S = 8.0

# (R2) full-mode per-frame capture: when full capture is armed, log a compact
# per-frame line for every deck report and lift the physical-source decode
# throttle.
FULL_FRAME_MIN = 0.0            # no throttle while full capture is armed

# (R3) InputPlumber journal classification regexes -> [IPJ-ADD]/[IPJ-REMOVE]/
# [IPJ-CREATE]/[IPJ-DESTROY]/[IPJ-SUSPEND] markers (greppable InputPlumber layer).
IPJ_ADD_RE = re.compile(
    r"(iio device added|source device added|Device added:|adding source device|"
    r"Found missing .* adding source|Spawning task to add)", re.IGNORECASE)
IPJ_REMOVE_RE = re.compile(
    r"(source device removed|Detected source device removed|device removed:|"
    r"Removing source|removed source)", re.IGNORECASE)
IPJ_CREATE_RE = re.compile(
    r"(Created target device|Requesting to create device|Got request to create|"
    r"Finished creating target device)", re.IGNORECASE)
IPJ_DESTROY_RE = re.compile(
    r"(destroy|stop list|Stopping target|Removing target|target device .* stop)",
    re.IGNORECASE)
IPJ_SUSPEND_RE = re.compile(
    r"(suspend|handle_suspend|clear_state|schedule_clear_state)", re.IGNORECASE)

# (R1/R9) InputPlumber journal lines that DECLARE the active gyro source
# (gyro_center arbiter / gyro-handle / iio center binding) — the logger mirrors
# these as [GYRO-SRC] so the timeline shows WHICH source became active + WHY.
GYRO_SRC_RE = re.compile(
    r"(gyro[-_]center|gyro[-_]handle|gyro_center|gyro-handle|CEN-GYRO|"
    r"center.*gyro|gyro.*center|force_iio|iio.*gyro|gyro.*iio|"
    r"right_gyro|left_gyro|arbiter)", re.IGNORECASE)

# (R1) iio device names that identify the ATTACHED CENTER body IMU (the gyro_3d
# sensor InputPlumber binds as the center / right_gyro slot source).
CENTER_IMU_NAME_HINTS = ("gyro_3d", "gyro", "imu")

# ---------------------------------------------------------------------------
# v3.5 — boot-id reader (called once by state.py at import).
# ---------------------------------------------------------------------------

def _read_boot_id():
    """First 8 chars of /proc/sys/kernel/random/boot_id (stable per boot)."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as _f:
            return _f.read().strip()[:8]
    except Exception:
        return "????????"

# ---------------------------------------------------------------------------
# udev monitor (daemon thread) — command (interest is dynamic, see udev.py)
# ---------------------------------------------------------------------------

UDEVADM_CMD = [
    "udevadm", "monitor", "--property", "--udev",
    "--subsystem-match=input", "--subsystem-match=hidraw", "--subsystem-match=iio",
    "--subsystem-match=hid", "--subsystem-match=usb",
]
