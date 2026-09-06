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

import os
import re
import time

import threading
import time

from . import state as S
from . import discover as D
from .conf import (DECK_MODE_WORDS, FULL_MODE_GAME, FULL_MODE_POST,
                   FULL_MODE_WAKE, LOOP_STALL_HB, LOOP_STALL_LOG_S,
                   MODE_TAG_WORDS, PAD_DELIVERY_HB, PAD_STALL_S,
                   REGRAB_MISSING_S, SESS_INTERVAL, WAKE_GYRO_COMPARE_S)
from .flow import flow_refresh, flow_scan
from .hidraw import _hid_label
from .logcore import log, run_cmd


# ---------------------------------------------------------------------------
# v2 — mode / attach-detach state tracker (role-based)
# ---------------------------------------------------------------------------

def _deck_mode(found_hid):
    """LEGACY transport-derived mode (v3.11 and earlier). KEPT ONLY as the
    internal STATE-tracker vocabulary (no-deck / gaming / desktop / transition)
    for the [STATE: steady-state ...] line. v3.12 (todo 52r FIX-A) does NOT use
    this for the SNAPSHOT mode= field or the [DETACH]/[DOCK] mode= tag — those
    now come from the REAL session-mode classifier (ipgyro.mode), because this
    transport view reports "gaming" whenever a uhid gaming deck exists, which is
    true even while the user sits in a DESKTOP session running ESO."""
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
    """(v3.12 FIX-A) The REAL session-mode word ('desktop'|'gaming'|'unknown')
    for the [DETACH]/[DOCK]/[RECONNECT-DT] mode= tag, from the ipgyro.mode
    classifier (a DESKTOP session present vs a no-desktop Big Picture/gamescope
    GAMING session). A running game NEVER changes this verdict — launching ESO
    from the desktop stays desktop. force=True re-classifies NOW so the tag is
    never a stale periodic verdict. The v3.11 transport/Steam-focus derivation
    is GONE from this path (it produced the gaming-mode lie)."""
    from . import mode as M   # lazy: mode.py imports only conf/logcore
    try:
        if force:
            return M.scan(time.monotonic(), force=True)
        return M.live()
    except Exception:
        return "unknown"


def _detach_marker(mode):
    """Byte-compatible [DETACH] line text (shared by capture.scan_hidraw and the
    startup-window synthesis so both always emit the identical format). The
    mode=/owner= payload is the v3.12 REAL session-mode word (FIX-A)."""
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
    """'desktop' -> 'desktop' / 'gaming' -> 'gaming-mode' / 'unknown' ->
    'unknown' for the mode= tag on [DETACH]/[DOCK]/[RECONNECT-DT] (v3.12
    FIX-A real session-mode vocabulary; 'game'/'none' legacy keys kept for
    byte-compat of any pre-v3.12 caller)."""
    return MODE_TAG_WORDS.get(m, m)


def _group_labels(role):
    """Sorted _hid_label() of every LIVE hidraw entry with `role`. Role-based,
    never label-prefix-based — labels are now human config words (FIX-1)."""
    return sorted(_hid_label(h) for h in S.HID_DEVICES.values()
                  if h.get("role") == role)


def _fmt_state(st):
    return ("mode={} session-mode={} controller=[{}] xbox-pad=[{}] iio={}"
            .format(
                _human_deck_mode(st["mode"]),
                _human_mode_tag(st.get("real", "unknown")),
                ",".join(st["legion_hid"]) or "-",
                ",".join(st["xpad"]) or "-", st["iio"]))


def state_tracker(devices, found_hid):
    """Diff the derived state against the previous scan and log transitions.
    v3.12 (todo 52r FIX-A): the STATE line's mode= now reflects the REAL
    session-mode verdict (desktop|gaming|unknown) alongside the transport deck
    view, so a desktop+ESO session is never labelled gaming-mode."""
    mode = _deck_mode(found_hid)          # transport view (STATE vocabulary)
    real = _live_mode_tag(force=False)    # REAL session-mode word (FIX-A)
    legion_hid = sorted(_hid_label(h) for h in found_hid.values()
                        if h.get("role") == D.ROLE_PHYSICAL)
    xpad = sorted(
        (d.get("name") or "").strip()
        for d in devices if "x-box pad" in (d.get("name") or "").lower())
    st = {"mode": mode, "real": real, "legion_hid": legion_hid, "xpad": xpad,
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
    if st["real"] != S.LAST_STATE["real"]:
        changes.append("session-mode {} -> {}".format(
            _human_mode_tag(S.LAST_STATE["real"]),
            _human_mode_tag(st["real"])))
    for key, noun in (("legion_hid", "controller hidraw"),
                      ("xpad", "kernel X-Box pad(evdev)")):
        old = set(S.LAST_STATE[key])
        new = set(st[key])
        if new - old:
            changes.append(f"{noun} attached: {', '.join(sorted(new - old))}")
        if old - new:
            changes.append(f"{noun} detached: {', '.join(sorted(old - old))}")
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
    controller=[...] (physical input) and virtual=[...] (Steam-facing output).
    v3.11 (todo 52o): adds the real-delivery counters deck_rd/s (deck frames,
    hop 1 only), pad_ev/s (EV events on the 28de:11ff pad), game_reader=yes/no
    (a game pid holds the pad fd) and gyro_map=pad-axes|mouse|none.
    v3.12 (todo 52r): mode= is the REAL session-mode word (desktop|gaming|
    unknown, FIX-A); adds sleep=<state> (FIX-B), the per-data-type pad counters
    pad_key/s + pad_abs/s (FIX-C), game_reader=<pid>:<name> (real game) and the
    per-source gyro carrier (FIX-C)."""
    mode = _human_mode_tag(_live_mode_tag(force=False))   # REAL session-mode
    cen = S.CEN_IMU
    cen_s = f"{cen['entry']}({cen['name']})" if cen else "none"
    controller = _group_labels(D.ROLE_PHYSICAL)
    virtual = _group_labels(D.ROLE_DECK)
    pad_node = S.PAD_NODE or "-"
    # -- sleep state word (FIX-B): entering|asleep|awake(+last duration) ------
    if S.SLEEP_STATE == "entering":
        sleep_s = "entering"
    elif S.SLEEP_STATE == "asleep":
        sleep_s = "asleep"
    else:
        sleep_s = "awake"
        if S.SLEEP_LAST_DUR is not None:
            sleep_s += f"+{S.SLEEP_LAST_DUR:.0f}s"
    # -- real game process (FIX) + pad fd holder (v3.13 split) ----------------
    # v3.13 (todo 52t #6): game_reader= now reports the REAL running game
    # process (e.g. eso64.exe — resolved from S.STEAM_PROC_SET minus Proton/wine
    # helpers); pad_holder= is the process ACTUALLY holding the pad fd (e.g.
    # winedevice.exe) — the proof-of-delivery reader behind [PAD→GAME].
    _mpid, _mname = _main_game_proc()
    gr = f"{_mpid}:{_mname}" if _mpid is not None else "none"
    _hpid, _hname = _pad_holder()
    holder = f"{_hpid}:{_hname}" if _hpid is not None else "none"
    gyro_carrier = S.GYRO_SRC_CARRIER or "unknown"
    # refresh the per-hop flow compact line (never raises) so a grep of this
    # snapshot alone shows the whole H0-H3 chain health (v3.13 #4).
    try:
        flow_refresh(time.monotonic())
    except Exception:
        pass
    flow_s = S.FLOW_COMPACT or "-"
    right_gyro = "yes" if S.RIGHT_HANDLE_PRESENT else "no"
    log(f"[SNAPSHOT] reason={reason} mode={mode} "
        f"controller=[{','.join(controller) or '-'}] "
        f"virtual=[{','.join(virtual) or '-'}] "
        f"iio={len(S.IIO_DEVICES)} cen_imu={cen_s} "
        f"gyro_src={S.GYRO_SRC or 'unknown'} gyro_carrier={gyro_carrier} "
        f"right_gyro={right_gyro} "
        f"steam_focus={S.STEAM_UI_FOCUS or 'none'} "
        f"steam_procs={S.STEAM_APP_SUMMARY or '(none)'} "
        f"game={'yes' if S.GAME_FOCUSED else 'no'} "
        f"deck_keepalive={'yes' if S.DECK_KEEPALIVE else 'no'} "
        f"pad_node={pad_node} "
        f"deck_rd/s={S.DECK_RD_COUNT} pad_key/s={S.PAD_KEY_COUNT} "
        f"pad_abs/s={S.PAD_ABS_COUNT} pad_gyro/s={S.PAD_GYRO_AXIS_COUNT} "
        f"game_reader={gr} pad_holder={holder} "
        f"gyro_map={S.PAD_GYRO_MAP or 'unknown'} flow={flow_s} "
        f"sleep={sleep_s}")


# ---------------------------------------------------------------------------
# v3.11 (todo 52o) — REAL input-delivery tracking. The game reads a SEPARATE
# virtual pad node Steam creates ("Microsoft X-Box 360 pad", VID:PID 28de:11ff)
# — deck frames (28de:12f0 -> Steam) are only HOP 1, never proof the game got
# the input. The ONLY host-side proof is EV events on the pad node WHILE the
# game process holds the pad fd (/proc/<pid>/fd -> /dev/input/eventN). Deck
# frames + [STEAM-CONFIG active=GAME] are PRECONDITIONS, never proof.
# ---------------------------------------------------------------------------

def _pad_reader_pids():
    """PIDs holding the pad fd open (/proc/<pid>/fd -> the pad eventN node).
    The game (ESO via Proton/SDL/evdev) is the reader that matters."""
    node = S.PAD_NODE
    pids = set()
    if not node:
        return pids
    try:
        procs = os.listdir("/proc")
    except Exception:
        return pids
    for p in procs:
        if not p.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{p}/fd")
        except Exception:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{p}/fd/{fd}")
            except Exception:
                continue
            if target == node:
                pids.add(int(p))
                break
    return pids


def _proc_environ_dict(pid):
    """Decoded environ dict of one pid ('' on any failure)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except Exception:
        return {}
    env = {}
    for kv in raw.split(b"\x00"):
        k, _, v = kv.partition(b"=")
        if k:
            env[k.decode("ascii", "replace")] = v.decode("utf-8", "replace")
    return env


def _proc_comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() or ""
    except Exception:
        return ""


# v3.13 (todo 52t #6) — the REAL running game process (e.g. eso64.exe) vs the
# process holding the pad fd (e.g. winedevice.exe). _game_reader_pid() resolves
# the pad-fd READER (for Proton games that is a wine child that inherited the
# pad fd); the actual game binary sits in S.STEAM_PROC_SET under the same
# SteamAppId. These helpers separate the two for the SNAPSHOT game_reader= /
# pad_holder= fields.
_HELPER_TOKENS = frozenset((
    "wine", "wineserver", "winedevice", "wineboot", "wineconsole", "winecfg",
    "explorer", "steamwebhelper", "reaper", "proton", "services", "plugplay",
    "rundll32", "start", "conhost", "crashpad", "steamclient", "steam",
    "bash", "sh", "python", "gamemode", "steamdeck", "vrserver",
))


def _looks_helper(comm):
    """True when a process comm is a Steam/Proton/wine/helper rather than the
    actual game binary (eso64.exe is NOT a helper; winedevice.exe IS)."""
    name = (comm or "").lower().replace(" ", "")
    if not name:
        return True
    if name in ("steam",):
        return True
    return any(tok in name for tok in _HELPER_TOKENS)


def _main_game_proc():
    """(v3.13/v3.15) Return (pid, comm) of the REAL running game process from
    S.STEAM_PROC_SET (pid -> (appid, name, cmd)), filtered to the appid of the
    resolved game reader when available and excluding Proton/wine/Steam helper
    processes — e.g. (54802, 'eso64.exe'). Purely /proc-derived (never a
    hardcoded game name). Returns (None, '') when no game process is
    detectable.

    v3.15 (todo 52z) FIX: the old "longest non-helper name wins" heuristic
    reported a wine service host (svchost.exe / bufferselfpatch / CrRendererMain)
    instead of the actual game binary (eso64.exe) — the user ran ESO and the
    SNAPSHOT game_reader= showed svchost. The REAL game is the process that owns
    the wine tree which reads the pad fd (the pad-fd reader's Steam-game
    ancestor). We resolve that FIRST (walk up from each pad-fd reader to a
    Steam game process) and only fall back to the longest-non-helper heuristic
    when no reader ancestor is found. This restores the v3.13 proof (H3=ok with
    the real reader) WITHOUT the v3.14 helper-exclusion regression in
    _game_reader_pid() (that function is untouched and still resolves the pad-fd
    reader itself)."""
    want = S.PAD_GAME_READER_APPID

    # -- 1: the real game = the Steam-game ancestor of a pad-fd reader --------
    # The pad-fd reader (winedevice.exe / the wine child holding the pad) is a
    # descendant of the actual game (eso64.exe). Walking up from the reader to a
    # Steam game process names the REAL game, never a wine service host.
    try:
        children = _proc_children_map()
        for pid in sorted(S.PAD_READER_PIDS):
            anc = _steam_game_ancestor(pid, children)
            if anc is None:
                continue
            _appid, name, _cmd = S.STEAM_PROC_SET.get(anc, (None, "", ""))
            if want and _appid != want:
                continue
            if _looks_helper(name):
                continue
            return (anc, name or "?")
    except Exception:
        pass

    # -- 2: fallback — longest non-helper name under the reader appid ---------
    best = (None, "")
    for pid in sorted(S.STEAM_PROC_SET):
        appid, name, _cmd = S.STEAM_PROC_SET[pid]
        if want and appid != want:
            continue
        if _looks_helper(name):
            continue
        if best[0] is None or len(name or "") > len(best[1] or ""):
            best = (pid, name or "?")
    if best[0] is None:      # fallback: no reader-appid filter
        for pid in sorted(S.STEAM_PROC_SET):
            _appid, name, _cmd = S.STEAM_PROC_SET[pid]
            if _looks_helper(name):
                continue
            if best[0] is None or len(name or "") > len(best[1] or ""):
                best = (pid, name or "?")
    return best


def _pad_holder():
    """(v3.13) Return (pid, comm) of the actual process holding the pad fd
    (/proc/<pid>/fd -> the pad node), e.g. (54485, 'winedevice.exe') — the
    proof-of-delivery reader behind [PAD→GAME]. The REAL game process is
    _main_game_proc(); for Proton games the fd holder is a wine child."""
    readers = sorted(S.PAD_READER_PIDS)
    if not readers:
        return None, ""
    pid = readers[0]
    return pid, _proc_comm(pid) or "?"


def _game_reader_pid():
    """(v3.12 FIX: REAL GAME PID) The ACTUAL game process holding the pad fd,
    not "any Steam process". Resolution order:
      1. a pad-fd reader that is itself a running Steam game (pid in
         S.STEAM_PROC_SET, i.e. its environ carries SteamAppId/SteamGameId);
      2. a pad-fd reader whose /proc environ carries a SteamAppId/SteamGameId
         (covers Proton/wine children that inherit the env but are not in the
         Steam proc set because they are not direct Steam-launched pids);
      3. a pad-fd reader that is a child (wine/proton) of a Steam game process
         (walk /proc/<pid>/task/<tid>/children up to the game) — e.g.
         winedevice.exe / ESO's wine tree holding the pad fd.
    Publishes S.PAD_READER_PIDS + the resolved game reader identity
    (S.PAD_GAME_READER_PID / _NAME / _APPID). Returns the game pid or None.
    Generic: the AppID is read from environ (ESO SteamAppId=306130 is
    discovered, never hardcoded)."""
    readers = _pad_reader_pids()
    S.PAD_READER_PIDS = readers
    if not readers:
        S.PAD_GAME_READER_PID = None
        S.PAD_GAME_READER_NAME = ""
        S.PAD_GAME_READER_APPID = None
        return None

    # -- 1: a reader that is itself a running Steam game ----------------------
    for pid in sorted(readers):
        if pid in S.STEAM_PROC_SET:
            _set_game_reader(pid)
            return pid
    # -- 2: a reader whose environ carries a SteamAppId ------------------------
    for pid in sorted(readers):
        env = _proc_environ_dict(pid)
        appid = env.get("SteamAppId") or env.get("SteamGameId")
        if appid:
            _set_game_reader(pid, appid=appid)
            return pid
    # -- 3: a reader that is a wine/proton child of a Steam game --------------
    # Build parent->children once and walk upward from each reader to find a
    # Steam game ancestor (the game owns the wine tree that reads the pad).
    children = _proc_children_map()
    for pid in sorted(readers):
        anc = _steam_game_ancestor(pid, children)
        if anc is not None:
            _set_game_reader(pid, appid=S.STEAM_PROC_SET.get(anc, (None,))[0])
            return pid
    S.PAD_GAME_READER_PID = None
    S.PAD_GAME_READER_NAME = ""
    S.PAD_GAME_READER_APPID = None
    return None


def _set_game_reader(pid, appid=None):
    """Publish the resolved real-game reader identity on S."""
    S.PAD_GAME_READER_PID = pid
    S.PAD_GAME_READER_NAME = _proc_comm(pid) or "?"
    if appid is None:
        appid = S.STEAM_PROC_SET.get(pid, (None,))[0]
    S.PAD_GAME_READER_APPID = appid


def _proc_children_map():
    """{pid -> set(child pids)} from /proc/<pid>/task/<tid>/children (best-effort).
    Reading a process's children requires the same uid OR root; the logger runs
    as root so every tree is readable. Returns a dict."""
    out = {}
    try:
        entries = os.listdir("/proc")
    except Exception:
        return out
    for p in entries:
        if not p.isdigit():
            continue
        try:
            tids = os.listdir(f"/proc/{p}/task")
        except Exception:
            continue
        for tid in tids:
            try:
                with open(f"/proc/{p}/task/{tid}/children") as f:
                    raw = f.read().split()
            except Exception:
                continue
            for c in raw:
                if c.isdigit():
                    out.setdefault(int(p), set()).add(int(c))
    return out


def _steam_game_ancestor(pid, children):
    """Walk upward from `pid` to the nearest ancestor that is a running Steam
    game (in S.STEAM_PROC_SET). Returns the ancestor pid or None. Uses the
    children map inverted (child -> parent) by scanning all parents."""
    # build child -> parent from the children map
    parent_of = {}
    for parent, kids in children.items():
        for k in kids:
            parent_of[k] = parent
    cur = pid
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        if cur in S.STEAM_PROC_SET:
            return cur
        cur = parent_of.get(cur)
    return None


def _gyro_map_verdict(now):
    """Infer how the active Steam config maps gyro, from pad EV evidence:
      * gyro-axis (ABS_RX/ABS_RY) events on the pad -> 'pad-axes' (PROVABLE)
      * no gyro-axis events but the deck IMU is moving and the pad is delivering
        buttons/sticks -> gyro is bound elsewhere (mouse) -> 'mouse' (NOT
        provable through the pad)
      * otherwise -> 'none' (no gyro evidence)."""
    if S.PAD_GYRO_AXIS_COUNT > 0:
        return "pad-axes"
    # deck IMU moving recently + pad delivering non-gyro events -> gyro bound
    # to a different output (mouse) -> not provable through the pad.
    deck_moving = S.DECK_MOTION_COUNT > 0
    pad_active = S.PAD_ABS_COUNT > 0 or S.PAD_EV_COUNT > 0
    if deck_moving and pad_active:
        return "mouse"
    return "none"


def _wake_gyro_compare(now):
    """(v3.12 FIX-C) Post-wake gyro-vs-button attribution. Called once per wake
    (guarded by S.WAKE_GYRO_COMPARED) ~WAKE_GYRO_COMPARE_S after the wake. It
    compares the PRE-SLEEP pad gyro/button baseline (captured at [SLEEP-ENTER])
    against the POST-WAKE pad flow and emits:
      * [GYRO-REGAINED]        pre-sleep gyro was flowing AND post-wake gyro is
                               flowing again (buttons also flowing) — gyro OK;
      * [GYRO-STALL-AFTER-WAKE] pre-sleep gyro was flowing but post-wake gyro is
                               NOT on the pad while buttons/sticks still reach
                               the game — the exact "gyro died after wake"
                               symptom, attributed to the WAKE boundary (never
                               to a detach);
      * (no marker)            no pre-sleep gyro baseline / no game reader, so
                               nothing provable to compare (honest silence).
    The per-source gyro carrier (center vs right-handle) is read from
    S.GYRO_SRC_CARRIER so the marker names WHICH physical gyro is live."""
    if S.PRE_SLEEP_GYRO is not True:
        return
    if S.PAD_GAME_READER_PID is None:
        return
    btn_now = S.PAD_KEY_COUNT > 0 or S.PAD_STICK_COUNT > 0
    gyro_now = S.PAD_GYRO_AXIS_COUNT > 0
    carrier = S.GYRO_SRC_CARRIER or "unknown"
    if gyro_now:
        if not S.WAKE_GYRO_STALLED:
            log(f"[GYRO-REGAINED] post-wake gyro-on-pad flowing again "
                f"(gyro={S.PAD_GYRO_AXIS_COUNT}/s, carrier={carrier}) — gyro "
                f"survived the wake")
    elif btn_now:
        S.WAKE_GYRO_STALLED = True
        log(f"[GYRO-STALL-AFTER-WAKE] pre-sleep gyro was on the pad but "
            f"post-wake gyro={S.PAD_GYRO_AXIS_COUNT}/s while buttons/sticks "
            f"still reach the game (pad_key/s={S.PAD_KEY_COUNT} "
            f"pad_abs/s={S.PAD_STICK_COUNT}) — gyro LOST at the WAKE boundary, "
            f"NOT at detach (carrier={carrier})")
    else:
        log(f"[GYRO-STALL-AFTER-WAKE] post-wake NO pad flow at all yet "
            f"(buttons={btn_now} gyro={gyro_now}) — game not re-delivering yet")


def pad_verdict_scan(now):
    """(v3.11/v3.12) Per-second REAL-delivery verdict. v3.12 (todo 52r FIX-C)
    models BUTTONS and GYRO as INDEPENDENT per-source streams with SEPARATE
    counters + anti-spam latches, so one stall never masks the other and the log
    can honestly show "buttons reached the game, gyro did not" after a wake.
    Emits:
      * [DECK→STEAM]  deck frames flowing (hop 1: deck -> Steam) — upstream
                      only, a PRECONDITION, never proof the game got the input;
      * [PAD→GAME]    pad EV events (28de:11ff) WHILE the REAL game pid holds
                      the pad fd — the ONLY host-side proof input reached the
                      game. Split per data type: buttons (pad_key/s) and
                      sticks (pad_abs/s) each get their OWN delivery latch so a
                      busy button stream can never starve the stick/gyro verdict
                      (the v3.11 shared-latch starvation bug);
      * [PAD-DEAD]    pad node + game reader present + deck delivering but ZERO
                      pad EV events for PAD_STALL_S (Valve steam-for-linux#13029:
                      game dropped the device and never re-acquired);
      * [GYRO→PAD-AXIS] gyro-driven ABS_RX/ABS_RY events on the pad while the
                      game holds it (gyro PROVABLY reached the game);
      * [GYRO-OUTPUT] gyro is bound to mouse/unmapped (not provable through the
                      pad) — never fabricate a delivery claim;
      * [GYRO-REGAINED]/[GYRO-STALL-AFTER-WAKE] once per wake (FIX-C) by
                      comparing the pre-sleep gyro baseline vs post-wake flow.
    [PAD-RECREATED]/[GAME-LOST-PAD] are emitted by devices.scan_evdev. Resets
    the per-second pad counters after the verdict."""
    deck_rd = S.DECK_RD_COUNT
    deck_mot = S.DECK_MOTION_COUNT
    deck_act = S.DECK_ACTION_COUNT
    pad_ev = S.PAD_EV_COUNT
    pad_abs = S.PAD_ABS_COUNT
    pad_key = S.PAD_KEY_COUNT
    pad_stick = S.PAD_STICK_COUNT
    pad_gyro = S.PAD_GYRO_AXIS_COUNT
    pad_node = S.PAD_NODE
    game_reader = _game_reader_pid()
    S.PAD_GAME_READER = game_reader is not None
    S.PAD_GYRO_MAP = _gyro_map_verdict(now)

    # -- hop-1: deck frames flowing (upstream only, precondition) ------------
    if deck_rd > 0 and pad_node:
        if now - S.PAD_DELIVERY_LAST >= PAD_DELIVERY_HB:
            S.PAD_DELIVERY_LAST = now
            log(f"[DECK→STEAM] deck delivering {deck_rd} rd/s "
                f"(mot={deck_mot} act={deck_act}) — HOP 1 deck->Steam only, "
                f"NOT proof the game got the input")

    # -- proof: pad EV events + the REAL game pid holds the pad fd ------------
    if game_reader is not None and pad_ev > 0:
        S.PAD_DEAD_LOGGED = False          # pad is alive -> clear [PAD-DEAD]
        gr = f"{game_reader}:{S.PAD_GAME_READER_NAME or '?'}"
        # -- buttons (EV_KEY) — its OWN latch (FIX-C) -------------------------
        if pad_key > 0:
            if now - S.PAD_KEY_LATCH >= PAD_DELIVERY_HB:
                S.PAD_KEY_LATCH = now
                log(f"[PAD→GAME] buttons pad_key/s={pad_key} on pad {pad_node} "
                    f"(28de:11ff) read by game {gr} — BUTTONS reached the game")
        # -- sticks/triggers (non-gyro EV_ABS) — its OWN latch (FIX-C) ---------
        if pad_stick > 0:
            if now - S.PAD_STICK_LATCH >= PAD_DELIVERY_HB:
                S.PAD_STICK_LATCH = now
                log(f"[PAD→GAME] sticks pad_abs/s={pad_stick} on pad {pad_node} "
                    f"(28de:11ff) read by game {gr} — STICKS reached the game")
        # -- gyro provability through the pad (independent of buttons) --------
        if pad_gyro > 0:
            if not S.PAD_GYRO_AXIS_LOGGED or \
                    now - S.PAD_GYRO_AXIS_LAST >= PAD_DELIVERY_HB:
                S.PAD_GYRO_AXIS_LOGGED = True
                S.PAD_GYRO_AXIS_LAST = now
                log(f"[GYRO→PAD-AXIS] {pad_gyro} gyro-axis ABS_RX/ABS_RY "
                    f"events/s on pad {pad_node} read by game {gr} "
                    f"— gyro PROVABLY reached the game (mapped to pad axes)")
        else:
            # deck IMU moving but no gyro on the pad -> gyro bound to mouse
            if deck_mot > 0 and S.PAD_GYRO_MAP == "mouse":
                if now - S.PAD_GYRO_AXIS_LAST >= PAD_DELIVERY_HB:
                    S.PAD_GYRO_AXIS_LAST = now
                    log(f"[GYRO-OUTPUT] deck IMU moving (mot={deck_mot}/s) but "
                        f"NO gyro on pad axes — gyro is bound to MOUSE output "
                        f"(different virtual node); NOT provable through the "
                        f"pad host-side")
    else:
        # -- [PAD-DEAD] (v3.13 fix #5): STRONG pad-dead only. The v3.12 branch
        # fired on the FIRST quiet 1 s scan although the text promised
        # PAD_STALL_S=5 consecutive seconds. Now the stall ACCUMULATES the
        # required consecutive quiet seconds (S.PAD_STALL_SECS) before firing,
        # and [PAD-DEAD] is NOT emitted when H2 routing is desktop (that is a
        # routing state handled by [STEAM-FOCUS-LOST-GAME], not a pad-dead
        # state). Only a STRONG [PAD-DEAD] (game config active + game reader
        # present + deck delivering + PAD_STALL_REQ consecutive seconds of 0
        # pad events) fires.
        routing_desktop = (S.STEAM_FOCUS_KIND in ("769", "413080", "OTHER")
                           or S.STEAM_FOCUS_KIND is None)
        if (game_reader is not None and pad_node and deck_rd > 0
                and pad_ev == 0 and not routing_desktop):
            S.PAD_STALL_SECS += 1
            if S.PAD_STALL_SECS >= PAD_STALL_S:
                if not S.PAD_DEAD_LOGGED or \
                        now - S.PAD_DEAD_LAST >= PAD_DELIVERY_HB:
                    S.PAD_DEAD_LOGGED = True
                    S.PAD_DEAD_LAST = now
                    log(f"[PAD-DEAD] pad {pad_node} present + game "
                        f"{game_reader}:{S.PAD_GAME_READER_NAME or '?'} holds "
                        f"it + deck delivering {deck_rd} rd/s but ZERO pad EV "
                        f"events for {S.PAD_STALL_SECS:.0f} consecutive s "
                        f"(game config active, focus={S.STEAM_FOCUS_KIND}) — "
                        f"game dropped the device and never re-acquired "
                        f"(Valve steam-for-linux#13029)")
        else:
            S.PAD_STALL_SECS = 0
            S.PAD_DEAD_LOGGED = False

    # -- (FIX-C) once-per-wake gyro-vs-button compare -------------------------
    if (S.SLEEP_WAKE_TS is not None and S.SLEEP_WAKE_TS > S.WAKE_GYRO_COMPARED
            and now - S.SLEEP_WAKE_TS >= WAKE_GYRO_COMPARE_S):
        S.WAKE_GYRO_COMPARED = S.SLEEP_WAKE_TS
        try:
            _wake_gyro_compare(now)
        except Exception:
            pass

    # -- (v3.13) per-hop flow proof: [GYRO-FLOW]/[BTNS-FLOW]/[STX-FLOW]/
    #    [FLOW]/[STEAM-FOCUS-LOST-GAME]/[STEAM-FOCUS-DROP-AFTER-WAKE].
    #    Runs on the same 1 s cadence (after the wake-compare so the pre-sleep
    #    focus baseline is already captured). Never raises.
    try:
        flow_scan(now)
    except Exception as e:
        log(f"LOGGER: flow scan error: {e}")

    # -- reset the per-second pad counters (consumed by the next snapshot) ----
    S.PAD_EV_COUNT = 0
    S.PAD_ABS_COUNT = 0
    S.PAD_KEY_COUNT = 0
    S.PAD_STICK_COUNT = 0
    S.PAD_GYRO_AXIS_COUNT = 0


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
    owner tag. v3.10 (D4): resets the [REGRAB-MISSING] latch and arms the
    re-grab watchdog so a dock that Steam NEVER re-binds is reported loudly
    after REGRAB_MISSING_S instead of staying silently absent for 55 minutes."""
    if S.DETACH_AT is not None:
        dt = now - S.DETACH_AT
        log(f"[RECONNECT-DT] detach->dock took {dt:.1f}s "
            f"mode={_human_mode_tag(_live_mode_tag())} owner=controller "
            f"(controller re-attached; Steam re-grab to be confirmed by "
            f"[STEAM-REGRAB]/[DECK-ALIVE])")
        S.DETACH_AT = None
    S.DOCK_AT = now
    S.REGRAB_DT_LOGGED = False
    S.REGRAB_MISSING_LOGGED = False
    _enter_full("dock")
    snapshot_state("dock")
    # D4 — arm the re-grab watchdog (fires [REGRAB-MISSING] if Steam never
    # re-binds the deck within REGRAB_MISSING_S of this dock).
    try:
        _spawn_regrab_watchdog(now)
    except Exception:
        pass


def mark_regrab(now, via):
    """(R8) Called when the game/Steam re-binds input after a dock (via =
    'steam-regrab' | 'deck-alive' | 'game-input'). Measures the dock ->
    game-binds-again latency as [REGRAB-DT] once per dock window. v3.10 (D4):
    a real re-grab also cancels the [REGRAB-MISSING] watchdog latch."""
    if S.DOCK_AT is None or S.REGRAB_DT_LOGGED:
        return
    dt = now - S.DOCK_AT
    S.REGRAB_DT_LOGGED = True
    S.REGRAB_MISSING_LOGGED = True   # a real re-grab means the deck WAS re-bound
    log(f"[REGRAB-DT] dock->{via} took {dt:.1f}s "
        f"(controller input re-bound after re-dock)")
    _enter_full("regrab")
    snapshot_state("regrab")


# ---------------------------------------------------------------------------
# v3.10 (todo 52l) — D4 [REGRAB-MISSING] watchdog + D7 [LOOP-STALL] watchdog.
# ---------------------------------------------------------------------------

def _spawn_regrab_watchdog(dock_at):
    """(D4) Start a daemon thread that, REGRAB_MISSING_S after the dock, logs
    [REGRAB-MISSING] if Steam never re-bound the deck (no [STEAM-REGRAB] /
    [DECK-ALIVE] -> mark_regrab never ran). Turns today's silent 55-minute
    absence into a loud, dated marker. Idempotent per dock window."""
    def _watch():
        deadline = dock_at + REGRAB_MISSING_S
        while not S.STOP:
            now = time.monotonic()
            if now >= deadline:
                # a NEWER dock superseded this one -> drop the stale watchdog
                if S.DOCK_AT is not None and S.DOCK_AT > dock_at:
                    return
                if not S.REGRAB_MISSING_LOGGED and not S.REGRAB_DT_LOGGED:
                    S.REGRAB_MISSING_LOGGED = True
                    log(f"[REGRAB-MISSING] Steam did not re-grab deck within "
                        f"{REGRAB_MISSING_S:.0f}s after dock "
                        f"(no [STEAM-REGRAB]/[DECK-ALIVE] since "
                        f"dock@{dock_at - S.BOOT_T0:.0f}s) — deck input may "
                        f"stay unbound")
                return
            time.sleep(0.5)
    try:
        threading.Thread(target=_watch, daemon=True).start()
    except Exception:
        pass


def reenum_check(now):
    """(D6) Called by capture.scan_hidraw on every physical [DETACH]/[DOCK]
    transition. When the physical controller comes back as a DIFFERENT interface
    than the one that detached (e.g. controller@1.1 -> controller@1.3, IMU ts
    reset, state=(attached,detached)), device identity changed across the wake
    -> emit an explicit [RE-ENUM] marker so the re-enumeration is never silent.
    `now` = the monotonic dock instant."""
    before = S.PHYS_ID_BEFORE_DETACH
    after = S.PHYS_ID_LAST
    if not before or not after or before == after:
        return
    S.PHYS_ID_BEFORE_DETACH = None
    log(f"[RE-ENUM] physical controller re-enumerated as a DIFFERENT interface "
        f"across the detach/dock: {before} -> {after} "
        f"(device identity changed; IMU/state may have reset)")


# ---------------------------------------------------------------------------
# D7 — [LOOP-STALL] watchdog thread. The cadence loop keeps S.LAST_TICK fresh
# (via pm_wake_check); this thread reports when the loop produces nothing for
# longer than LOOP_STALL_LOG_S, so a stall is logged as a CAUSE rather than
# appearing only as missing data. [LOOP-RECOVERED] marks the end of the stall.
# ---------------------------------------------------------------------------

def loop_stall_tick(now):
    """(D7) Called by the cadence loop every pass (through pm_wake_check) to
    keep S.LAST_TICK fresh. Clears the in-stall latch when the loop resumes."""
    if S.LOOP_STALL_ACTIVE:
        S.LOOP_STALL_ACTIVE = False
        if time.monotonic() - S.LOOP_STALL_LAST_LOG >= LOOP_STALL_HB:
            S.LOOP_STALL_LAST_LOG = time.monotonic()
            log(f"[LOOP-RECOVERED] main loop producing again after "
                f"{now - S.LAST_TICK:.1f}s stall")
    S.LAST_TICK = now


def loop_stall_watchdog_loop():
    """(D7) Daemon thread: every ~1 s check how long the cadence loop has been
    silent (S.LAST_TICK). When it exceeds LOOP_STALL_LOG_S, log [LOOP-STALL]
    (re-asserted at most every LOOP_STALL_HB) and set S.LOOP_STALL_ACTIVE so the
    loop's next tick logs [LOOP-RECOVERED]. Runs in a daemon thread (the pm/ipj
    threads already prove the threaded-watchdog pattern works)."""
    while not S.STOP:
        now = time.monotonic()
        silent = now - S.LAST_TICK
        if silent >= LOOP_STALL_LOG_S:
            if not S.LOOP_STALL_ACTIVE:
                S.LOOP_STALL_ACTIVE = True
                S.LOOP_STALL_LAST_LOG = now
                log(f"[LOOP-STALL] main loop silent {silent:.1f}s "
                    f"(daemon threads stay alive; capture may be stalled)")
            elif now - S.LOOP_STALL_LAST_LOG >= LOOP_STALL_HB:
                S.LOOP_STALL_LAST_LOG = now
                log(f"[LOOP-STALL] main loop still silent "
                    f"({silent:.0f}s and counting)")
        time.sleep(1.0)


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
    (snapshot + full capture) exactly once via SLEEP_WAKE_TS/WAKE_HANDLED_TS.
    v3.10 (D2/D3/D5/D6): every genuine wake also spawns the post-wake dense
    capture thread (fallback if the follower already ran it — spawn is
    idempotent), and the loop tick feeds the D7 [LOOP-STALL] watchdog."""
    if S.SLEEP_PENDING and S.LAST_TICK and (now - S.LAST_TICK) > 5.0:
        # genuine resume: journal-confirmed enter + whole-process freeze. The
        # follower had not yet consumed the journal wake line before our tick.
        S.SLEEP_PENDING = False
        S.SLEEP_WAKE_TS = now
        S.WAKE_HANDLED_TS = now
        S.SLEEP_STATE = "awake"
        if S.SLEEP_ENTER_MONO is not None:
            S.SLEEP_LAST_DUR = now - S.SLEEP_ENTER_MONO
        log(f"PM: [SLEEP-WAKE] real resume — monotonic gap "
            f"{now - S.LAST_TICK:.1f}s (systemd-suspend journal enter was "
            f"seen before the freeze; wake confirmed by the gap)")
        _enter_full("sleep-wake", hold=FULL_MODE_WAKE)
        snapshot_state("sleep-wake")
        _spawn_wake_burst_fallback(S.SLEEP_WAKE_TS)
    elif S.SLEEP_WAKE_TS is not None and S.SLEEP_WAKE_TS > S.WAKE_HANDLED_TS:
        # the journal follower logged the real [SLEEP-WAKE] line; apply the
        # wake consequences here (once) so full capture re-arms after resume.
        S.WAKE_HANDLED_TS = S.SLEEP_WAKE_TS
        S.SLEEP_STATE = "awake"
        _enter_full("sleep-wake", hold=FULL_MODE_WAKE)
        snapshot_state("sleep-wake")
        _spawn_wake_burst_fallback(S.SLEEP_WAKE_TS)
    loop_stall_tick(now)


def _spawn_wake_burst_fallback(wake_mono):
    """(D2/D3/D5/D6) Spawn the post-wake dense capture thread from the main-loop
    backstop. The journal follower (pm.py) normally spawns it first on the
    [SLEEP-WAKE] line; this is the fallback when the follower's line was not
    consumed before our tick. spawn_wake_burst is idempotent (one window per
    wake), so calling it here too is safe."""
    try:
        from . import wake as W  # lazy: keep module-load order cycle-free
        W.spawn_wake_burst(wake_mono)
    except Exception:
        pass


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


# ---------------------------------------------------------------------------
# v3.12 (todo 52r FIX-A) — periodic REAL session-mode re-scan. Keeps the
# SNAPSHOT mode= field and the [MODE ... via ...] change marker fresh from the
# ipgyro.mode classifier (desktop|gaming|unknown) without touching system state.
# ---------------------------------------------------------------------------

def mode_scan(now):
    """Recompute the REAL session-mode verdict (ipgyro.mode.classify) and log
    [MODE <word> via <evidence>] on change. Called on the MODE_SCAN_INTERVAL
    cadence from app.main(). Never raises."""
    from . import mode as M   # lazy: mode.py imports only conf/logcore
    try:
        M.scan(now, force=False)
    except Exception:
        pass
