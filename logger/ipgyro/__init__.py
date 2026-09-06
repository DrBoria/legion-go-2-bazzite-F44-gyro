# -*- coding: utf-8 -*-
"""ipgyro — pure-logger-internal package for ip-gyro-logger (the passive
Legion Go 2 gyro diagnostic daemon). The monolithic logger/ip-gyro-logger.py
was split into this package on 2026-09-05 (todo 51c) with EXACTLY preserved
behaviour (every log-line format, marker and cadence); the thin launcher
logger/ip-gyro-logger.py keeps the original entry path so the systemd unit and
install.sh are untouched.

Submodules: conf (constants/regexes/tables), state (shared mutable globals),
logcore (log/rotation), devices (proc/evdev/session), iio (IMU sampling),
udev (udevadm monitor), hidraw (deck+source monitor/decode), obs (state/
snapshot/full-mode/PM/game orchestration), ipj (InputPlumber journal mirror),
steam (Steam registry/controller_ui/procs), pm (reliable systemd-suspend
journal follower), gyro (center-IMU identity + gyro-source arbiter verdict),
app (main loop).

v3.6 (todo 51d "ЛОГ ВСЁ" audit) added pm.py + gyro.py and the new markers
[CEN-IMU]/[GYRO-SRC]/[DEV-INV]/[FRAME]/[IPJ-ADD|REMOVE|CREATE|DESTROY|SUSPEND]/
[GAME-BIND]/[GAME-LOSS]/[REGRAB-DT] plus the reliable [SLEEP-ENTER]/[SLEEP-WAKE]
follower. This __init__ imports nothing heavy on purpose.

v3.7 (todo 52b) — single-source generic refactor: config.py (tomllib
/etc/ip-gyro-logger/config.toml, [labels] never VID/PID), codecs.py (decode/
probe/fmt registry keyed by frame-header signature), discover.py (role registry
physical-source / deck-output by transport+codec sniff, no device ids),
capture.py (per-device role+codec pipeline; hidraw.py is now a thin facade),
udev/obs/gyro/steam/ipj/app re-wired role-based + G1 (Steam-side
grab/release/regrab), G2 (mode/owner tags), G3 (consumer-delivery probe),
G4 (real-PID IPJ watchdog).

v3.8 (todo 52e) — desktop-detach VISIBILITY fix (a real no-game desktop detach
was invisible/mislabeled): F1 mode=game|desktop now comes from the LIVE Steam
context (focused AppID / active Steam-Input window + running-game proc set) at
the moment of a [DETACH]/[DOCK]/[RECONNECT-DT], with the transport-based
deck_mode_of only as the fallback when Steam context is unavailable; F2 the
startup blind window is closed (udev-recorded physical churn is replayed and
[STARTUP-WINDOW] synthesized before the cadence loop); F3 a fresh Steam poll is
forced at every physical change; F4 game=yes means the ACTIVE Steam-Input
window is a game (focused AppID), so lingering ESO/Proton/launcher procs no
longer keep game=yes after the user is back on the desktop shell.

v3.9 (todo 52f) — two user-driven clarity fixes:
 FIX-1 CLEAR NAMING: the cryptic output/config vocabulary (DECK-GAME /
 LEGION-SRC / DECK-DESK labels, mode=GAMING, "baseline", legion=/deck= fields)
 is replaced with human words (labels controller / steam-deck /
 steam-deck-old / steam-controller, mode=gaming-mode|desktop, "steady-state",
 controller=/virtual= fields). Marker TOKENS stay byte-compatible.
 FIX-2 REAL SLEEP: [SLEEP-ENTER]/[SLEEP-WAKE] now fire ONLY on a journal-
 confirmed suspend/resume (systemd-suspend phrases "Performing sleep
 operation ..." / "System returned from sleep operation ...") instead of the
 false mono-gap [SLEEP-WAKE] that fired every ~24-33 s main-loop stall.

v3.10 (todo 52l) — SLEEP-IN-GAME VISIBILITY (D1-D7). The ~24-33 s main-loop
stalls were forensically WHOLE-PROCESS freezes (all daemon threads silent too)
from log() blocking on its stdout->journald mirror under heavy full-capture
output. D1 root fix: stdout is O_NONBLOCK with bounded writes (a full pipe
drops mirror lines, never freezes the process). D2/D3/D5/D6: a dedicated
post-wake dense-capture thread (ipgyro.wake) samples deck hidraw + physical
XInput right-gyro + center IIO at 0.1 s for 30-60 s after every real
[SLEEP-WAKE], INDEPENDENT of the cadence loop, and emits [GYRO-FEED] verdicts
at ~2 s/~10 s, post-wake [CEN-IMU]/[GYRO-SRC] at 1 s, an [IPJ-WAKE] wake-
ordered flush, and [RE-ENUM] on interface change. D4: [REGRAB-MISSING]
watchdog after dock. D7: [LOOP-STALL] watchdog thread. New markers
([GYRO-FEED]/[REGRAB-MISSING]/[RE-ENUM]/[LOOP-STALL]/[WAKE-BURST]/[IPJ-WAKE]/
[LOOP-RECOVERED]) only ADD; all existing marker tokens stay byte-compatible.

v3.11 (todo 52o) — CORRECT INPUT-DELIVERY TRACKING. The old v3.10 claim treated
deck frames (28de:12f0 -> Steam) + [STEAM-CONFIG active=GAME] as proof the
input reached the GAME — wrong: that only proves deck->Steam (hop 1). Steam
Input reads the deck and creates a SEPARATE game-facing virtual pad
("Microsoft X-Box 360 pad", VID:PID 28de:11ff, uinput evdev) that the GAME
(ESO via Proton/XInput/SDL/evdev) actually reads. The ONLY host-side proof of
game delivery is EV events on that pad node WHILE the game process holds the
pad fd (/proc/<pid>/fd -> /dev/input/eventN). v3.11 adds a stable pad identity
(devices.py: name prefix + 28de:11ff, NOT the churning eventN), the proof
marker [PAD→GAME] (pad EV + game reader), the Valve steam-for-linux#13029
lifecycle markers [PAD-DEAD]/[PAD-RECREATED]/[GAME-LOST-PAD], the gyro
provability split ([GYRO→PAD-AXIS] when gyro lands on pad ABS_RX/ABS_RY vs
[GYRO-OUTPUT] when it is bound to mouse/unmapped and NOT provable through the
pad), the [DECK→STEAM] hop-1 re-anchor tag, and new SNAPSHOT counters
(deck_rd/s, pad_ev/s, game_reader, gyro_map). Deck frames + Steam config are
PRECONDITIONS only — never proof. New markers only ADD; every existing marker
token stays byte-compatible.

v3.12 (todo 52r "FIX LOGGER") — TRUTHFUL mode/sleep/per-source delivery. Three
user ground-truth corrections fixed the v3.11 lies at their SOURCE:
  FIX-A REAL MODE: mode= is no longer derived from the transport deck roles or
    the Steam-Input focused window (which reported gaming-mode while the user
    sat in a DESKTOP session running ESO). ipgyro.mode classifies the REAL
    session mode: DESKTOP = a desktop session is present (DE shell process /
    XDG_CURRENT_DESKTOP / graphical loginctl session on a seat); GAMING = a
    no-desktop Big Picture/gamescope session. A running game NEVER switches
    mode. [MODE desktop|gaming|unknown via <evidence>] at each change; SNAPSHOT
    mode=desktop|gaming|unknown.
  FIX-B AUTHORITATIVE SLEEP: the systemd-suspend journal follower is hardened
    AND a second independent kernel-PM signal (journalctl -k) is added, so the
    user's real suspend ALWAYS appears as [SLEEP-ENTER]/[SLEEP-WAKE] with wall
    timestamps + sleep duration; SNAPSHOT sleep=entering|asleep|awake+<dur>.
  FIX-C PER-SOURCE DELIVERY: buttons and gyro are INDEPENDENT per-source
    streams with SEPARATE counters + anti-spam latches (pad_key/s, pad_abs/s,
    pad_gyro/s) so one stall never masks the other; the per-source gyro carrier
    (center while attached, right-handle while detached) is tracked; gyro loss
    is attributed to the WAKE boundary ([GYRO-REGAINED]/[GYRO-STALL-AFTER-WAKE]
    vs the pre-sleep baseline), never to a detach. game_reader is the REAL game
    (<pid>:<name> via SteamAppId + wine/proton tree), not "any Steam process".
  PART 2: the evdev-visible hops (Steam pad 28de:11ff) move onto python-evdev
    (ipgyro.evreader: stable identity, churn-following reopen, independent
    bounded non-blocking reader); the physical XInput hop stays raw-hidraw.
    python-evdev is optional (falls back to the raw devices.py capture).

v3.13 (todo 52t) — PER-HOP FLOW PROOF. The user rejects the v3.12 «не
доказуемо» cell for the wake-loss (gyro stops in-game after wake) and demands
the logger PROVE WHERE in the chain the gyro (and any data type) is lost, hop
by hop, with evidence on every line. New ipgyro.flow models the H0-H3 chain
per data type: H0 physical source (center-body IMU in the XInput right_gyro
slot / right-handle 17ef:61ed) -> H1 deck->Steam (deck mot/s + act/s) -> H2
Steam ROUTING (active controller config/focus = the running game AppID vs
desktop 413080 — the routing hop) -> H3 Steam pad 28de:11ff -> the game reader
(gyro-axis ABS_RX/ABS_RY / pad_key / pad_abs). New markers: [GYRO-FLOW]/
[BTNS-FLOW]/[STX-FLOW] (per-hop verdicts naming the FIRST lost hop with quoted
evidence), the compact [FLOW ...] snapshot line, [STEAM-FOCUS-LOST-GAME] (a
running game is detectable but Steam's config focus is not that game — a
PROVEN routing loss, never vague), and [STEAM-FOCUS-DROP-AFTER-WAKE] (game was
Steam-focused before sleep, config focus is desktop after wake — the loss is
attributed to wake + routing, never to detach). [PAD-DEAD] now ACCUMULATES the
required consecutive quiet seconds and is suppressed when H2 routing is
desktop. SNAPSHOT adds pad_gyro/s, the real game process (game_reader=eso64.exe)
vs the pad-fd holder (pad_holder=winedevice.exe), and the flow= chain. New
markers only ADD; every existing marker token stays byte-compatible.

v3.15 (todo 52z) — CORRECTED REBUILD on the v3.13 base. The over-engineered
v3.14 (todo 52x) was REVERTED (user: "последний твой коммит ревертнуть, ты там
чет лишнего нагородил и сделать нормально") — it introduced a game_reader
regression (helper-exclusion rejected the real wine pad holder -> H3 always
lost) and wrong detach/attach/carrier tracking. v3.15 keeps the correct v3.13
per-hop flow proof and applies ONLY the minimal ground-truth fixes:
  * game_reader reports the REAL game (eso64.exe), not a wine service host
    (svchost/bufferselfpatch) — obs._main_game_proc resolves the pad-fd
    reader's Steam-game ancestor first (user ran ESO and it showed svchost).
  * a [DETACH] at a sleep-wake boundary while the controller was ATTACHED at
    [SLEEP-ENTER] is a SUSPEND RE-ENUMERATION (suppressed as [DETACH-SUPPRESSED]),
    not a real detach — the user's ground truth: "Sleep был точно с attached".
  * the gyro carrier is derived from the PHYSICAL ATTACH STATE (right handle
    17ef:61ed detached = right-handle carrier), not the always-present center
    IIO — the user's ground truth: when he detached, the RIGHT handle gyro
    worked (GyroR), not center. SNAPSHOT adds right_gyro=yes|no.

v3.16 (todo 53e) — BINDING-AWARE GYRO + MOUSE OBSERVER + STICKS TRACKING.
Research 53d showed gyro is a SEPARATE binding whose OUTPUT is decided by the
ACTIVE config (pad-axes right-stick | mouse | disabled): after wake Steam
reverts to the desktop config (413080) which re-binds gyro to MOUSE, so pad
ABS_RX/ABS_RY stops while buttons/sticks (still pad outputs) keep reaching the
game. v3.16 changes:
  * bind.py parses the ACTIVE config's gyro binding from the per-appid
    controller_*.vdf (the group bound to "gyro active" in the Default preset)
    -> pad-axes|mouse|disabled|unknown ([GYRO-BINDING]); never guesses.
  * buttons/sticks stay PAD-DOMAIN (pad-only verdict); gyro is BINDING-AWARE:
    pad-axes -> pad ABS_RX/ABS_RY, mouse -> mouse-injection motion ([GYRO→MOUSE]/
    [GYRO-STALL]), disabled -> verdict none (a config fact). config=413080 while
    a game runs = GYRO RE-DOMAIN to the desktop/mouse config, NOT a global loss.
  * false button loss fixed: "no game reader resolved" after wake is an
    OBSERVATION GAP (reader pid changed) — pad live + carrying events + a game
    context + a reader ever held the pad fd => H3 ok with
    reader=unresolved(pid-changed), never lost.
  * Steam pad churn after wake survived: on [PAD-RECREATED] the new pad fd is
    re-opened immediately and [PAD-DEAD] is suppressed until the full stall
    window (PAD_STALL_S) elapses AFTER the re-bind (S.PAD_REBIND_UNTIL).
  * H1=lost only when deck-mot AND pad-gyro AND mouse-motion are ALL 0 while
    the physical gyro is provably moving.
  * mouseobs.py counts REL_X/REL_Y on the auto-detected virtual pointer node
    (or extest-injected desktop motion) -> proves gyro->mouse output.
  * sticks/joysticks as an EXPLICIT type: left stick ABS_X/ABS_Y vs right stick
    ABS_RX/ABS_RY — honestly noted as SHARED with gyro-as-right-stick when the
    config binds gyro to pad-axes. SNAPSHOT adds gyro_binding, mouse,
    mouse_rel/s, stk_l/s + stk_r/s(rstick=physical|gyro-shared).
"""

__version__ = "v3.16"
