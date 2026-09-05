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
from here — obs never imports steam, so there is no import cycle. obs DOES
import _hid_label from ipgyro.hidraw (hidraw only lazily imports obs inside
scan_hidraw(), keeping module load order cycle-free).

Shared mutable state is ALWAYS accessed as S.<name> (see ipgyro.state).
"""

import time

from . import state as S
from . import discover as D
from .conf import FULL_MODE_GAME, FULL_MODE_POST, FULL_MODE_WAKE, SESS_INTERVAL
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


def _current_mode_tag():
    """'game'|'desktop'|'none' from the deck-output currently open (G2 mode tag
    for [DETACH]/[DOCK]/[RECONNECT-DT])."""
    for e in S.HID_DEVICES.values():
        m = D.deck_mode_of(e)
        if m:
            return m
    return "none"


def _fmt_state(st):
    return ("mode={} legion_hid=[{}] xpad=[{}] iio={}".format(
        st["mode"], ",".join(st["legion_hid"]) or "-",
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
        log("STATE: baseline " + _fmt_state(st))
        S.LAST_STATE = st
        return
    if st == S.LAST_STATE:
        return
    changes = []
    if st["mode"] != S.LAST_STATE["mode"]:
        changes.append(f"mode {S.LAST_STATE['mode']} -> {st['mode']}")
    for key, noun in (("legion_hid", "physical-source hidraw"),
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
    (R1/R9) also states the center IMU identity + active gyro source."""
    srcs = sorted(_hid_label(e) for e in S.HID_DEVICES.values())
    mode = _deck_mode(S.HID_DEVICES)
    cen = S.CEN_IMU
    cen_s = f"{cen['entry']}({cen['name']})" if cen else "none"
    phys = [l for l in srcs if l.startswith("LEGION")]
    deck = [l for l in srcs if l.startswith("DECK")]
    log(f"[SNAPSHOT] reason={reason} mode={mode} "
        f"legion=[{','.join(phys) or '-'}] "
        f"deck=[{','.join(deck) or '-'}] "
        f"iio={len(S.IIO_DEVICES)} cen_imu={cen_s} "
        f"gyro_src={S.GYRO_SRC or 'unknown'} "
        f"steam_focus={S.STEAM_UI_FOCUS or 'none'} "
        f"steam_procs={S.STEAM_APP_SUMMARY or '(none)'} "
        f"game={'yes' if S.GAME_RECORD else 'no'} "
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
            f"mode={_current_mode_tag()} owner=physical-source "
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
    srcs = sorted(_hid_label(e) for e in S.HID_DEVICES.values())
    deck = [l for l in srcs if l.startswith("DECK")]
    phys = [l for l in srcs if l.startswith("LEGION")]
    cen = S.CEN_IMU
    gyro = f"center-iio:{cen['entry']}" if cen else "none"
    if not cen:
        # fall back: any physical-source XInput carries gyro bytes
        if any(e.get("role") == D.ROLE_PHYSICAL
               for e in S.HID_DEVICES.values()):
            gyro = "physical-xinput"
    return (f"bound_controller=[{','.join(deck) or '-'}] "
            f"source=[{','.join(phys) or '-'}] gyro={gyro} "
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
    """(PM) The logger process is frozen during suspend, so a monotonic gap
    > 5s between main-loop ticks means we just woke up: emit [SLEEP-WAKE] with
    the duration, snapshot and re-arm full capture."""
    if S.LAST_TICK and now - S.LAST_TICK > 5.0:
        dur = now - S.LAST_TICK
        log(f"PM: [SLEEP-WAKE] monotonic gap {dur:.1f}s — resumed from suspend")
        _enter_full("sleep-wake", hold=FULL_MODE_WAKE)
        snapshot_state("sleep-wake")
    S.LAST_TICK = now


def pm_probe():
    """(PM, Слой 0) Throttled one-shot journal probe for systemd sleep
    enter/wake lines, surfaced as [PM] [SLEEP-ENTER]/[SLEEP-WAKE] (the enter
    side cannot be logged from the frozen process itself)."""
    now = time.monotonic()
    if now - S.LAST_PM_PROBE < SESS_INTERVAL:
        return
    S.LAST_PM_PROBE = now
    out = run_cmd(["journalctl", "-n", "25", "--no-pager", "-o", "short-iso",
                   "--since=-45s"], timeout=5)
    if not out:
        return
    for line in out.splitlines():
        low = line.lower()
        if "entering sleep state" in low or "suspending system" in low:
            if not S.SLEEP_PENDING:
                S.SLEEP_PENDING = True
                log("PM: [SLEEP-ENTER] " + line.strip())
        elif "system resumed" in low or ("restored" in low
                                         and "sleep" in low):
            if S.SLEEP_PENDING:
                S.SLEEP_PENDING = False
                log("PM: [SLEEP-WAKE] " + line.strip())
                _enter_full("sleep-wake", hold=FULL_MODE_WAKE)
                snapshot_state("sleep-wake")
