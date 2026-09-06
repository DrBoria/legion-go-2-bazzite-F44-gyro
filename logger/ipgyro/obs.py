# -*- coding: utf-8 -*-
"""
ipgyro.obs — the v2/v3.5 "observability orchestration" helpers that interpret
the union of evdev + hidraw + IIO/Steam/PM signals as human-readable state:
the mode/attach state tracker, [SNAPSHOT], the (e) full-capture window
([FULL-ON]), the [DETACH]/[DOCK] consequences ([RECONNECT-DT]), the PM
sleep/wake path ([SLEEP-ENTER]/[SLEEP-WAKE]) and the [GAME-START]/[GAME-STOP]
session boundary.

Role-based (todo 52a C3): mode/attach decisions use the GENERIC roles from
ipgyro.discover (deck-output game/desktop vs physical-source), never device
ids. G2 (todo 52b) adds a mode=desktop|game + owner tag to [DETACH]/[DOCK]/
[RECONNECT-DT] so the config-owner of a transition is explicit.

Steam-layer markers ([STEAM-STATE]/[STEAM-CONFIG]/[STEAM-GRAB/RELEASE/REGRAB])
live in ipgyro.steam, which imports _enter_full/snapshot_state/_game_boundary
from here. v3.8 (todo 52e): obs imports steam only LAZILY inside the
_live_mode_tag()/startup_reconcile() bodies (steam.py imports obs at module
level), so module-load order stays cycle-free. obs DOES import _hid_label from
ipgyro.hidraw (hidraw only lazily imports obs inside scan_hidraw(), keeping
module load order cycle-free).

Shared mutable state is ALWAYS accessed as S.<name> (see ipgyro.state).
"""

import time

from . import state as S
from . import discover as D
from .conf import (DECK_MODE_WORDS, FULL_MODE_GAME, FULL_MODE_POST,
                   FULL_MODE_WAKE, MODE_TAG_WORDS, SESS_INTERVAL)
from .hidraw import _hid_label
from .logcore import log, run_cmd


# ---------------------------------------------------------------------------
# v2 — mode / attach-detach state tracker (role-based)
# ---------------------------------------------------------------------------

def _deck_mode(found_hid):
    """Mode from the deck-output roles present (NOT pids): a GAMING (uhid) deck
    and/or a DESKTOP (vhci) Steam Controller."""
    game = any(D.is_game_deck(h) for h in found_hid.values())
    desk = any(h.get("role") == D.ROLE_DECK
               and h.get("deck_kind") == D.DECK_DESK
               for h in found_hid.values())
    if game and desk:
        return "TRANSITION-GAMING+DESKTOP"
    if game:
        return "GAMING"
    if desk:
        return "DESKTOP"
    return "NO-DECK"


def _live_mode_tag(force=False):
    """(v3.8 F1/F3) 'game'|'desktop'|'none' for the [DETACH]/[DOCK]/
    [RECONNECT-DT] mode= tag. The LIVE Steam context is authoritative: the
    focused AppID / active Steam-Input window (a real game AppID = game; the
    desktop shell 413080 / Steam client 769 / none = desktop) PLUS the
    running-game proc set (fallback only when focus is unknown). force=True
    runs a fresh Steam poll NOW (F3) so the tag is never a stale periodic
    [STEAM-STATE]. The transport-based deck_mode_of is used ONLY when the Steam
    context is unavailable (verdict None)."""
    from . import steam as ST   # lazy: steam.py imports obs at module level
    try:
        if force:
            ST.steam_refresh(time.monotonic())
        else:
            ST.record_mode_verdict()
    except Exception:
        pass
    if S.MODE_CTX is not None:
        return S.MODE_CTX
    # Steam context unavailable -> transport fallback (deck_output kind)
    for e in S.HID_DEVICES.values():
        m = D.deck_mode_of(e)
        if m:
            return m
    return "none"


def _detach_marker(mode):
    """Byte-compatible [DETACH] line text (shared by capture.scan_hidraw and the
    startup-window synthesis so both always emit the identical format). The
    mode=/owner= payload is the v3.9 human vocabulary (FIX-1)."""
    return (f"[DETACH] controller input GONE "
            f"(0 controller nodes open) mode={_human_mode_tag(mode)} "
            f"owner=controller — controller detached / off / "
            f"mode switch ([UDEV] shows the USB detail)")


def _dock_marker(mode, phys_n):
    """Byte-compatible [DOCK] line text (shared by capture.scan_hidraw and the
    startup-window synthesis so both always emit the identical format)."""
    return (f"[DOCK] controller input BACK "
            f"({phys_n} controller nodes open) mode={_human_mode_tag(mode)} "
            f"owner=controller — controller docked / re-attached")


def _human_deck_mode(m):
    """'GAMING' -> 'gaming-mode' etc. — the deck_mode() internal codes are only
    ever compared/diffed internally; what a user reads is always translated
    (v3.9 FIX-1 clear naming)."""
    return DECK_MODE_WORDS.get(m, m)


def _human_mode_tag(m):
    """'game' -> 'gaming-mode' / 'desktop' -> 'desktop' for the mode= tag on
    [DETACH]/[DOCK]/[RECONNECT-DT] (v3.9 FIX-1 human vocabulary)."""
    return MODE_TAG_WORDS.get(m, m)


def _group_labels(role):
    """Sorted _hid_label() of every LIVE hidraw entry with `role`. Role-based,
    never label-prefix-based — labels are now human config words (FIX-1)."""
    return sorted(_hid_label(h) for h in S.HID_DEVICES.values()
                  if h.get("role") == role)


def _fmt_state(st):
    return ("mode={} controller=[{}] xbox-pad=[{}] iio={}".format(
        _human_deck_mode(st["mode"]),
        ",".join(st["legion_hid"]) or "-",
        ",".join(st["xpad"]) or "-", st["iio"]))


def state_tracker(devices, found_hid):
    """Diff the derived state against the previous scan and log transitions."""
    mode = _deck_mode(found_hid)
    legion_hid = sorted(_hid_label(h) for h in found_hid.values()
                        if h.get("role") == D.ROLE_PHYSICAL)
    xpad = sorted(
        (d.get("name") or "").strip()
        for d in devices if "x-box pad" in (d.get("name") or "").lower())
    st = {"mode": mode, "legion_hid": legion_hid, "xpad": xpad,
          "iio": len(S.IIO_DEVICES)}
    if S.LAST_STATE is None:
        log("STATE: steady-state " + _fmt_state(st))
        S.LAST_STATE = st
        return
    if st == S.LAST_STATE:
        return
    changes = []
    if st["mode"] != S.LAST_STATE["mode"]:
        changes.append("mode {} -> {}".format(
            _human_deck_mode(S.LAST_STATE["mode"]),
            _human_deck_mode(st["mode"])))
    for key, noun in (("legion_hid", "controller hidraw"),
                      ("xpad", "kernel X-Box pad(evdev)")):
        old = set(S.LAST_STATE[key])
        new = set(st[key])
        if new - old:
            changes.append(f"{noun} attached: {', '.join(sorted(new - old))}")
        if old - new:
            changes.append(f"{noun} detached: {', '.join(sorted(old - new))}")
    if st["iio"] != S.LAST_STATE["iio"]:
        changes.append(f"iio devices {S.LAST_STATE['iio']} -> {st['iio']}")
    # (d/e) every mode/attach state transition arms full capture and snapshots
    _enter_full("state-change", hold=None)
    snapshot_state("state-change")
    log("STATE: " + "; ".join(changes) + "  [" + _fmt_state(st) + "]")
    S.LAST_STATE = st


# ---------------------------------------------------------------------------
# v3.5 — V22 full-observability helpers (contract a–e; full contract in
# Agent.md, section "2026-09-05 ~22:3x — V22 ДИЗАЙН-ДОКУМЕНТ ...").
# ---------------------------------------------------------------------------

def _full_mode():
    """(e) Full capture is active while a game runs or inside a transition
    window (FULL_RECORD_UNTIL)."""
    return S.GAME_RECORD or time.monotonic() < S.FULL_RECORD_UNTIL


def _enter_full(reason, hold=None):
    """(e) Arm the full-capture window for `hold` (default FULL_MODE_POST)
    seconds and log the entry once. Transition bursts only extend the window,
    never shorten it."""
    now = time.monotonic()
    if hold is None:
        hold = FULL_MODE_POST
    was_on = S.FULL_RECORD_UNTIL > now or S.GAME_RECORD
    S.FULL_RECORD_UNTIL = max(S.FULL_RECORD_UNTIL, now + hold)
    if not was_on and now - S.LAST_FULL_LOG >= 1.0:
        S.LAST_FULL_LOG = now
        log(f"[FULL-ON] full capture armed by {reason} "
            f"(hold {hold:.0f}s — iiO@0.1s + per-frame raw)")


def snapshot_state(reason):
    """(d) One loud [SNAPSHOT reason=...] line capturing every observable layer
    NOW, so any before/after transition diff shows exactly what changed.
    (R1/R9) also states the center IMU identity + active gyro source. v3.9
    (FIX-1): human mode word + role-based grouping under human field names
    controller=[...] (physical input) and virtual=[...] (Steam-facing output)."""
    mode = _human_deck_mode(_deck_mode(S.HID_DEVICES))
    cen = S.CEN_IMU
    cen_s = f"{cen['entry']}({cen['name']})" if cen else "none"
    controller = _group_labels(D.ROLE_PHYSICAL)
    virtual = _group_labels(D.ROLE_DECK)
    log(f"[SNAPSHOT] reason={reason} mode={mode} "
        f"controller=[{','.join(controller) or '-'}] "
        f"virtual=[{','.join(virtual) or '-'}] "
        f"iio={len(S.IIO_DEVICES)} cen_imu={cen_s} "
        f"gyro_src={S.GYRO_SRC or 'unknown'} "
        f"steam_focus={S.STEAM_UI_FOCUS or 'none'} "
        f"steam_procs={S.STEAM_APP_SUMMARY or '(none)'} "
        f"game={'yes' if S.GAME_FOCUSED else 'no'} "
        f"deck_keepalive={'yes' if S.DECK_KEEPALIVE else 'no'}")


def _mark_detach(now):
    """(Слой 1/3) [DETACH] consequences: remember the instant (for reconnect
    dt), snapshot AFTER the event, arm full capture across the re-dock window.
    The mode/owner tag lives on the [DETACH] line itself (capture.scan_hidraw)."""
    S.DETACH_AT = now
    _enter_full("detach")
    snapshot_state("detach")


def _mark_dock(now):
    """(Слой 1/3) [DOCK] consequences: measure reconnect dt since [DETACH],
    snapshot AFTER the event, arm full capture. (R8) also anchors DOCK_AT so the
    dock -> game-binds-again latency ([REGRAB-DT]) can be measured when Steam
    re-grabs / the deck stream resumes. G2: [RECONNECT-DT] carries the mode +
    owner tag."""
    if S.DETACH_AT is not None:
        dt = now - S.DETACH_AT
        log(f"[RECONNECT-DT] detach->dock took {dt:.1f}s "
            f"mode={_human_mode_tag(_live_mode_tag())} owner=controller "
            f"(controller re-attached; Steam re-grab to be confirmed by "
            f"[STEAM-REGRAB]/[DECK-ALIVE])")
        S.DETACH_AT = None
    S.DOCK_AT = now
    S.REGRAB_DT_LOGGED = False
    _enter_full("dock")
    snapshot_state("dock")


def mark_regrab(now, via):
    """(R8) Called when the game/Steam re-binds input after a dock (via =
    'steam-regrab' | 'deck-alive' | 'game-input'). Measures the dock ->
    game-binds-again latency as [REGRAB-DT] once per dock window."""
    if S.DOCK_AT is None or S.REGRAB_DT_LOGGED:
        return
    dt = now - S.DOCK_AT
    S.REGRAB_DT_LOGGED = True
    log(f"[REGRAB-DT] dock->{via} took {dt:.1f}s "
        f"(controller input re-bound after re-dock)")
    _enter_full("regrab")
    snapshot_state("regrab")


def _game_had_payload():
    """(R7) What the game HAD at the boundary: the bound controller (deck /
    physical-source hidraw labels) + the working gyro source (center IIO vs
    physical XInput)."""
    deck = _group_labels(D.ROLE_DECK)      # what the game BOUND (Steam output)
    phys = _group_labels(D.ROLE_PHYSICAL)  # the controller feeding it
    cen = S.CEN_IMU
    gyro = f"center-iio:{cen['entry']}" if cen else "none"
    if not cen:
        # fall back: any physical-source XInput carries gyro bytes
        if any(e.get("role") == D.ROLE_PHYSICAL
               for e in S.HID_DEVICES.values()):
            gyro = "physical-xinput"
    return (f"output=[{','.join(deck) or '-'}] "
            f"controller=[{','.join(phys) or '-'}] gyro={gyro} "
            f"steam_focus={S.STEAM_UI_FOCUS or 'none'}")


def _game_boundary(summary, start):
    """(Слой 3) [GAME-START]/[GAME-STOP] + full capture for the whole game
    session (the contract's "full in gaming sessions" behaviour). (R7) each
    boundary states what the game HAD (bound controller + working gyro) so the
    data-loss question «какие данные игра получила/потеряла» is answerable."""
    now = time.monotonic()
    had = _game_had_payload()
    if start:
        S.GAME_RECORD = True
        log(f"[GAME-START] running Steam games: {summary} — full capture ON "
            f"until the game exits")
        log(f"[GAME-BIND] game STARTED with {had}")
        _enter_full("game-start", hold=FULL_MODE_GAME)
        snapshot_state("game-start")
    else:
        S.GAME_RECORD = False
        log(f"[GAME-STOP] running Steam games: {summary} — full capture ends "
            f"(transition window {FULL_MODE_POST:.0f}s)")
        log(f"[GAME-LOSS] game STOPPED; had {had}")
        _enter_full("game-stop")
        snapshot_state("game-stop")


def pm_wake_check(now):
    """(PM, v3.9 FIX-2) The mono-gap backstop now fires ONLY for a GENUINE
    suspend: S.SLEEP_PENDING is True only after the systemd-suspend journal
    follower (pm.py) confirmed a real [SLEEP-ENTER], so a plain main-loop stall
    (~24-33 s periodic IIO/HIDFLOW stalls while the daemon threads stay alive)
    can NEVER emit a false [SLEEP-WAKE]. During a real suspend the whole
    process freezes, so the first tick here after resume shows a big monotonic
    gap WHILE SLEEP_PENDING is still True — that is the genuine-gap backstop.
    If the follower already consumed the resume line first (it clears
    SLEEP_PENDING and stamps S.SLEEP_WAKE_TS), we apply the wake consequences
    (snapshot + full capture) exactly once via SLEEP_WAKE_TS/WAKE_HANDLED_TS."""
    if S.SLEEP_PENDING and S.LAST_TICK and (now - S.LAST_TICK) > 5.0:
        # genuine resume: journal-confirmed enter + whole-process freeze. The
        # follower had not yet consumed the journal wake line before our tick.
        S.SLEEP_PENDING = False
        S.SLEEP_WAKE_TS = now
        S.WAKE_HANDLED_TS = now
        log(f"PM: [SLEEP-WAKE] real resume — monotonic gap "
            f"{now - S.LAST_TICK:.1f}s (systemd-suspend journal enter was "
            f"seen before the freeze; wake confirmed by the gap)")
        _enter_full("sleep-wake", hold=FULL_MODE_WAKE)
        snapshot_state("sleep-wake")
    elif S.SLEEP_WAKE_TS is not None and S.SLEEP_WAKE_TS > S.WAKE_HANDLED_TS:
        # the journal follower logged the real [SLEEP-WAKE] line; apply the
        # wake consequences here (once) so full capture re-arms after resume.
        S.WAKE_HANDLED_TS = S.SLEEP_WAKE_TS
        _enter_full("sleep-wake", hold=FULL_MODE_WAKE)
        snapshot_state("sleep-wake")
    S.LAST_TICK = now


def pm_probe():
    """(PM, Слой 0, v3.9 FIX-2) RECOVERY-ONLY poller: does nothing unless a
    journal-confirmed enter is pending (S.SLEEP_PENDING) — i.e. the pre-freeze
    follower armed a real sleep but its resume line was not consumed (follower
    restart / journald hiccup). It then scans the recent systemd-suspend
    journal for the WAKE that resolves it. It can NEVER emit a spurious
    [SLEEP-ENTER] (enter detection belongs to the pre-freeze follower) and
    never duplicates (once resolved pending is False, later polls are no-ops),
    so steady-state stalls produce nothing."""
    now = time.monotonic()
    if now - S.LAST_PM_PROBE < SESS_INTERVAL:
        return
    S.LAST_PM_PROBE = now
    if not S.SLEEP_PENDING:
        return                       # no journal-confirmed sleep to resolve
    from . import pm as PM   # lazy: keep module-load order cycle-free
    out = run_cmd(["journalctl", "-u", PM.PM_JOURNAL_UNIT, "-n", "30",
                   "--no-pager", "-o", "short-iso", "--since=-60s"], timeout=5)
    if not out:
        return
    for line in out.splitlines():
        if PM._classify(line) == "wake":
            PM._pm_emit(line)        # clears pending + stamps S.SLEEP_WAKE_TS
            return


# ---------------------------------------------------------------------------
# v3.8 (todo 52e F2) — closing the STARTUP BLIND WINDOW.
# ---------------------------------------------------------------------------

def startup_reconcile(now):
    """(v3.8 F2) Close the STARTUP BLIND WINDOW. Physical-source churn that
    happened after the logger's first scan baseline (S.PHYS_STATE set by the
    startup scan_hidraw) but BEFORE the main loop went live only appeared as
    raw [UDEV] remove/add lines — the aggregate [DETACH]/[DOCK] diff never ran
    in that gap (startup keeps the main thread busy with codec-sniff banners /
    journal backlogs), so a detach+re-dock fully inside the window was silently
    lost. We replay the churn the udev thread recorded (S.STARTUP_CHURN) and
    SYNTHESIZE the missing [DETACH]/[DOCK] markers (with the live mode tag, F1)
    so real events are captured; when synthesis is not possible we still emit an
    explicit [STARTUP-WINDOW] marker so a pre-baseline detach is never silently
    lost. Runs ONCE after startup init, before the cadence loop (sets
    S.TRACK_LIVE so the udev recorder stops buffering)."""
    if S.TRACK_LIVE:
        return
    S.TRACK_LIVE = True
    churn = list(S.STARTUP_CHURN)
    S.STARTUP_CHURN = []
    baseline = S.STARTUP_BASELINE_TS
    # events before the first baseline are "baked into" that baseline (nothing
    # to diff); only events at/after the baseline could have been missed.
    pre = [e for e in churn if baseline is None or e["ts"] < baseline]
    late = [e for e in churn if baseline is not None and e["ts"] >= baseline]

    # (F1/F3) one forced fresh Steam poll -> the live mode for every marker
    # (a real game AppID = game; desktop shell 413080 / Steam UI 769 = desktop).
    mode = _live_mode_tag(force=True) if late else _live_mode_tag(force=False)

    syn = 0
    state = S.PHYS_STATE
    for e in late:
        if e["kind"] == "detach" and state:
            state = False
            S.PHYS_STATE = False
            log(_detach_marker(mode))
            _mark_detach(e["ts"])        # anchors DETACH_AT to the real instant
            syn += 1
        elif e["kind"] == "dock" and state is False:
            state = True
            S.PHYS_STATE = True
            phys_n = sum(1 for x in S.HID_DEVICES.values()
                         if x.get("role") == D.ROLE_PHYSICAL)
            log(_dock_marker(mode, phys_n or 1))
            _mark_dock(e["ts"])          # dt measured against the real detach
            syn += 1
    S.PHYS_STATE = state

    b = max(0.0, (baseline or now) - S.BOOT_T0)
    if syn:
        pre_n = (f"; {len(pre)} churn event(s) before the +{b:.0f}s baseline "
                 f"not diffable") if pre else ""
        log(f"[STARTUP-WINDOW] {syn} [DETACH]/[DOCK] marker(s) SYNTHESIZED from "
            f"{len(late)} pre-loop physical churn event(s) recorded by udev "
            f"(tracking went live at +{b:.0f}s; the init gap would have lost "
            f"them){pre_n}")
    elif churn:
        log(f"[STARTUP-WINDOW] {len(churn)} pre-loop physical churn event(s) "
            f"recorded but no state change to synthesize (tracking live at "
            f"+{b:.0f}s baseline)")
    else:
        log(f"[STARTUP-WINDOW] detach tracking live at +{b:.0f}s baseline; "
            f"no pre-live physical churn recorded")
