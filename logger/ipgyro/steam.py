# -*- coding: utf-8 -*-
"""
ipgyro.steam — the Steam-layer capture: the virtual-gamepad registry
(virtualgamepadinfo.txt), the controller registration log (controller.txt),
Steam's per-app controller_ui.txt (direction D, Steam Input activation) and
the running-game processes read from /proc/<pid>/environ.

The Steam-layer markers live here: [STEAM-STATE] (periodic verdict),
[STEAM-CONFIG] (unified active config), [STEAM-GRAB]/[STEAM-RELEASE]/
[STEAM-REGRAB] (controller_ui connect/disconnect). Game boundaries are handed
off to obs._game_boundary ([GAME-START]/[GAME-STOP]) — steam imports
_enter_full/snapshot_state/_game_boundary from ipgyro.obs; obs never imports
steam, so there is no import cycle.

Shared mutable state (STEAM_CACHE/STEAM_UI_FOCUS/STEAM_APP_SUMMARY/
STEAM_PROC_SET/GAME_RECORD/DECK_KEEPALIVE) is accessed ONLY as S.<name>.
"""

import hashlib
import os
import re
import time

from . import state as S
from .conf import (
    CTL_RE,
    STEAM_TAIL_BYTES,
    STEAM_UI_CAND_RE,
    STEAM_UI_FOCUS_RE,
    STEAM_UI_MAX_LINES,
    STEAM_UI_NOISE_RE,
    STEAM_UI_TAIL_BYTES,
)
from .logcore import log
from .obs import _enter_full, _game_boundary, mark_regrab, snapshot_state


def _steam_bases():
    """Locate desktop-user homes (logger runs as root, Steam data is in /home)."""
    bases = []
    try:
        homes = sorted(os.listdir("/home"))
    except Exception:
        return bases
    for h in homes:
        base = os.path.join("/home", h)
        try:
            st = os.stat(base)
        except Exception:
            continue
        if getattr(st, "st_uid", 0) >= 1000:
            bases.append(base)
    return bases


def steam_scan(now):
    """Diff the Steam registry + controller log + controller_ui (per-app Steam
    Input activation, direction D) for every desktop user, and scan the running
    Steam game processes."""
    bases = _steam_bases()
    if not bases:
        return
    for base in bases:
        _steam_registry(os.path.join(
            base, ".local/share/Steam/config/virtualgamepadinfo.txt"))
        _steam_controller_log(os.path.join(
            base, ".local/share/Steam/logs/controller.txt"))
        _steam_controller_ui_log(os.path.join(
            base, ".local/share/Steam/logs/controller_ui.txt"))
    _steam_procs()
    # (v3.8 F1/F4) after every scan keep the live mode verdict fresh so the
    # periodic [STEAM-STATE]/[SNAPSHOT] game= tag and any transport-fallback
    # mode tag read the current focused-window context.
    record_mode_verdict()


def _steam_registry(path):
    exists = os.path.exists(path)
    sig = None
    content = []
    if exists:
        try:
            with open(path, "r", errors="replace") as f:
                content = f.read().splitlines()
            sig = hashlib.md5(
                "\n".join(content).encode("utf-8", "replace")).hexdigest()
        except Exception as e:
            log(f"STEAM: cannot read registry {path}: {e}")
            return
    cur = S.STEAM_CACHE.get(path)
    if cur is None:
        cur = {"reg_sig": None}
        S.STEAM_CACHE[path] = cur
    first = cur["reg_sig"] is None
    if sig == cur["reg_sig"]:
        return
    cur["reg_sig"] = sig
    if first:
        if exists:
            log(f"STEAM: registry present at start "
                f"({len(content)} lines): {path}")
        else:
            log(f"STEAM: registry absent at start (Steam has not registered "
                f"the deck controller yet): {path}")
    elif not exists:
        log(f"STEAM: registry REMOVED (will be re-created on next Steam "
            f"controller registration): {path}")
    if exists:
        # Parse real Steam format: "[slot N]" blocks with key=value lines.
        slots, cur = [], None
        for l in content:
            s = l.strip()
            if not s:
                continue
            if s.startswith("[") and s.endswith("]"):
                cur = {"slot": s, "name": "", "vid": "", "pid": "",
                       "handle": "", "type": ""}
                slots.append(cur)
            elif cur and "=" in s:
                k, _, v = s.partition("=")
                cur[k.strip().lower()] = v.strip()
        deck = [sl for sl in slots
                if sl.get("vid", "").lower().replace("0x", "") == "28de"
                or "deck" in sl.get("name", "").lower()
                or "controller" in sl.get("name", "").lower()]
        pidmap = {
            0x12F0: "FULL gaming deck 12f0 (gyro-capable)",
            0x12FB: "STALE 12fb (A/B only, dead gyro)",
            0x1205: "desktop Steam Controller 1205",
        }
        detail = []
        headline = "no 28de entry"
        for sl in deck:
            ps = sl.get("pid", "")
            try:
                pid_i = int(ps, 16) if ps else 0
            except ValueError:
                pid_i = 0
            verdict = pidmap.get(pid_i, f"pid={ps or '?'}")
            if headline == "no 28de entry":
                headline = verdict
            detail.append(
                f"{sl.get('slot')} name={sl.get('name') or '?'} "
                f"VID={sl.get('vid') or '?'} PID={sl.get('pid') or '?'} "
                f"handle={sl.get('handle') or '?'} type={sl.get('type') or '?'} "
                f"-> {verdict}")
        log(f"STEAM: registry lines={len(content)} verdict={headline} "
            f"28de_slots={len(deck)}")
        for d in detail:
            log(f"STEAM   | {d}")


def _steam_controller_log(path):
    cur = S.STEAM_CACHE.get(path)
    if cur is None:
        cur = {"ctl_pos": 0, "ctl_present": None}
        S.STEAM_CACHE[path] = cur
    try:
        size = os.path.getsize(path)
    except OSError:
        if cur.get("ctl_present") is not False:
            cur["ctl_present"] = False
            log(f"STEAM: controller log not present yet: {path}")
        return
    if cur.get("ctl_present") is not True:
        cur["ctl_present"] = True
        cur["ctl_pos"] = size  # start from the end: only NEW lines get logged
        log(f"STEAM: controller log present, size={size} "
            f"(tail-only from now): {path}")
        return
    pos = cur.get("ctl_pos", 0)
    if size < pos:           # log rotated/truncated -> re-read from start
        pos = 0
    if size == pos:
        return
    if size - pos > STEAM_TAIL_BYTES:
        pos = size - STEAM_TAIL_BYTES
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(pos)
            newtext = f.read()
    except Exception:
        return
    cur["ctl_pos"] = size
    shown = 0
    extra = 0
    for line in newtext.splitlines():
        if CTL_RE.search(line):
            if shown < 10:
                log("STEAM controller: " + line.strip())
                shown += 1
            else:
                extra += 1
    if extra:
        log(f"STEAM controller: +{extra} more matching lines this scan")


def _steam_ui_focus(kind, appid):
    """Human-readable focus label for a controller_ui OnFocusWindowChanged."""
    if kind == "game window type":
        return f"focused AppID {appid} (GAME window — Steam Input on the game)"
    if appid == "769":
        return "focused AppID 769 (Steam client UI)"
    if appid == "413080":
        return "focused AppID 413080 (Steam Big Picture / desktop shell)"
    return f"focused AppID {appid} ({kind})"


# ---------------------------------------------------------------------------
# v3.8 (todo 52e) — LIVE game-mode context (F1/F3/F4). The mode=game|desktop tag
# for [DETACH]/[DOCK]/[RECONNECT-DT] and the game=yes tag are now driven by the
# ACTIVE Steam-Input window (focused AppID), with the running-game proc set only
# as the fallback when no focus event has been seen yet. The transport-based
# deck_mode_of (discover.py) is used only when this whole verdict is None.
# ---------------------------------------------------------------------------

def _focus_appid_kind(focus):
    """Parse a S.STEAM_UI_FOCUS label into (appid, kind). kind is one of
    'GAME' | '769' | '413080' | 'OTHER' | None (None = no focus label yet).
    'GAME' = an actual game AppID with Steam Input on its window (the ONLY
    "game" signal per F4); 769 = Steam client UI, 413080 = Big Picture /
    desktop shell, OTHER = a non-Steam-Input window -> all desktop."""
    if not focus:
        return None, None
    m = re.search(r"\bAppID (\d+)", focus)
    appid = m.group(1) if m else None
    if "GAME window" in focus:
        return appid, "GAME"
    if appid == "769":
        return appid, "769"
    if appid == "413080":
        return appid, "413080"
    return appid, ("OTHER" if appid else None)


def _set_focus(focus):
    """Store the focus label AND its parsed AppID/kind together (the parsed
    fields feed the live mode verdict without re-parsing every call)."""
    S.STEAM_UI_FOCUS = focus
    S.STEAM_FOCUS_APPID, S.STEAM_FOCUS_KIND = _focus_appid_kind(focus)


def record_mode_verdict():
    """(v3.8 F1/F4) Recompute + store the LIVE mode verdict from the current
    Steam focus + running-game proc set and return (mode, detail):
      * focused AppID is a real game window   -> ('game', ...)
      * focused AppID is 769 / 413080 / OTHER -> ('desktop', ...)  — even when
        game/Proton procs are still alive (F4: game = the ACTIVE Steam-Input
        window, not mere process presence)
      * no focus event yet, but game procs run -> ('game', ...)  [startup]
      * no focus and no procs (Steam scanned)  -> ('desktop', ...)
      * Steam context entirely unavailable     -> (None, ...)   [transport
        fallback in the caller]
    Also refreshes S.STEAM_FOCUS_APPID/KIND + S.GAME_FOCUSED + S.MODE_CTX/
    S.MODE_CTX_DETAIL so every tag source reads the same live verdict."""
    appid, kind = _focus_appid_kind(S.STEAM_UI_FOCUS)
    summary = S.STEAM_APP_SUMMARY
    procs = bool(summary and summary != "(none)")
    if kind == "GAME":
        mode, game, det = ("game", True,
                           f"focus AppID {appid} (active Steam-Input game window)")
    elif kind in ("769", "413080", "OTHER"):
        mode, game, det = ("desktop", False,
                           f"focus AppID {appid} (Steam UI / desktop shell)")
    elif procs:
        mode, game, det = ("game", True,
                           f"running-game procs {summary} (no focus event yet)")
    elif summary is not None:
        mode, game, det = ("desktop", False, "no running Steam game procs")
    else:
        mode, game, det = (None, False,
                           "no Steam focus and no proc data (context unavailable)")
    S.STEAM_FOCUS_APPID = appid
    S.STEAM_FOCUS_KIND = kind
    S.MODE_CTX = mode
    S.MODE_CTX_DETAIL = det
    S.GAME_FOCUSED = game
    return mode, det


def steam_refresh(now):
    """(v3.8 F3) FORCE a fresh Steam poll NOW (registry + controller_ui tail +
    running procs) instead of waiting for the next periodic STEAM_SCAN_INTERVAL
    tick, then recompute the live mode verdict. Called at every physical
    [DETACH]/[DOCK] so the mode= tag uses LIVE focus, never a stale periodic
    [STEAM-STATE]. Returns the (mode, detail) verdict."""
    steam_scan(now)
    return record_mode_verdict()[:2]


def _steam_ui_focus_from_text(lines):
    """Most recent OnFocusWindowChanged event in `lines` -> label or None."""
    last = None
    for line in lines:
        m = STEAM_UI_FOCUS_RE.search(line)
        if m:
            last = _steam_ui_focus(m.group(1), m.group(2))
    return last


def _steam_ui_focus_from_tail(path, size):
    """Read the file tail (up to STEAM_UI_TAIL_BYTES) purely for the startup
    baseline focus label."""
    try:
        with open(path, "r", errors="replace") as f:
            if size > STEAM_UI_TAIL_BYTES:
                f.seek(size - STEAM_UI_TAIL_BYTES)
            return _steam_ui_focus_from_text(f.read().splitlines())
    except Exception:
        return None


def _steam_controller_ui_log(path):
    """v3.1 — tail-diff Steam's controller_ui.txt (per-app Steam Input
    activation evidence, direction D). Filtered to per-game config loads, focus
    events and controller identity; logs a compact FOCUS marker on AppID change
    only. Nothing before logger start is replayed."""
    cur = S.STEAM_CACHE.get(path)
    if cur is None:
        cur = {"ui_pos": 0, "ui_present": None}
        S.STEAM_CACHE[path] = cur
    try:
        size = os.path.getsize(path)
    except OSError:
        if cur.get("ui_present") is not False:
            cur["ui_present"] = False
            log(f"STEAM UI: controller_ui log not present yet: {path}")
        return
    if cur.get("ui_present") is not True:
        cur["ui_present"] = True
        cur["ui_pos"] = size   # start from the end: only NEW lines get logged
        log(f"STEAM UI: controller_ui log present, size={size} "
            f"(tail-only from now): {path}")
        focus = _steam_ui_focus_from_tail(path, size)
        if focus:
            _set_focus(focus)
            log("STEAM UI FOCUS: baseline " + focus)
        return
    pos = cur.get("ui_pos", 0)
    if size < pos:             # rewritten/rotated -> start from scratch
        pos = 0
    if size == pos:
        return
    if size - pos > STEAM_UI_TAIL_BYTES:
        pos = size - STEAM_UI_TAIL_BYTES
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(pos)
            newtext = f.read()
    except Exception:
        return
    cur["ui_pos"] = size
    newlines = newtext.splitlines()
    # (Слой 2) [STEAM-GRAB]/[STEAM-RELEASE]/[STEAM-REGRAB] verdicts FIRST, so a
    # controller connect/disconnect is never missed even if focus did not change
    _steam_controller_verdicts(newlines)
    # -- compact focus transition (only when the focused AppID actually changed)
    focus = _steam_ui_focus_from_text(newlines)
    if focus and focus != S.STEAM_UI_FOCUS:
        _set_focus(focus)
        log("STEAM UI FOCUS: " + focus)
        _steam_config_marker(focus)   # (Слой 2) unified [STEAM-CONFIG] active
    # -- raw filtered evidence lines (bounded, noise excluded) ----------------
    shown = 0
    extra = 0
    for line in newlines:
        if (STEAM_UI_CAND_RE.search(line)
                and not STEAM_UI_NOISE_RE.search(line)):
            if shown < STEAM_UI_MAX_LINES:
                log("STEAM UI: " + line.strip())
                shown += 1
            else:
                extra += 1
    if extra:
        log(f"STEAM UI: +{extra} more matching lines this scan")


def _steam_procs():
    """v3.1 — running game processes launched by Steam, read from the
    SteamAppId/SteamGameId env of every /proc/<pid>/environ. Logs pid-level
    add/end lines plus a compact summary when the active game set changes."""
    procs = {}                 # pid -> (appid, name, cmd)
    games = {}                 # appid -> [pids]
    try:
        entries = os.listdir("/proc")
    except Exception:
        return
    for p in entries:
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/environ", "rb") as f:
                data = f.read()
        except Exception:
            continue
        appid = None
        for kv in data.split(b"\x00"):
            k, _, v = kv.partition(b"=")
            if k in (b"SteamAppId", b"SteamGameId"):
                appid = v.decode("ascii", "replace").strip() or None
                break
        if not appid:
            continue
        name = ""
        cmd = ""
        try:
            with open(f"/proc/{p}/comm") as f:
                name = f.read().strip()
        except Exception:
            pass
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(
                    "utf-8", "replace").strip()
        except Exception:
            pass
        procs[p] = (appid, name, cmd[:160])
        games.setdefault(appid, []).append(p)
    # -- pid-level transitions -------------------------------------------------
    old = S.STEAM_PROC_SET
    for p in sorted(set(procs) - set(old)):
        appid, name, cmd = procs[p]
        log(f"STEAM PROC: AppID {appid} running pid={p} name={name} "
            f"cmd={cmd or '(no cmdline)'}")
    for p in sorted(set(old) - set(procs)):
        appid, name, _cmd = old[p]
        log(f"STEAM PROC: AppID {appid} process ended (pid={p} name={name})")
    for p in sorted(set(old) & set(procs)):
        if procs[p][0] != old[p][0]:
            log(f"STEAM PROC: pid={p} AppID {old[p][0]} -> {procs[p][0]}")
    S.STEAM_PROC_SET = procs
    # -- compact summary when the active set changes ---------------------------
    summary = ", ".join(f"{a}[{len(ps)}]" for a, ps in sorted(games.items()))
    if not summary:
        summary = "(none)"
    if summary != S.STEAM_APP_SUMMARY:
        had_game = S.STEAM_APP_SUMMARY not in (None, "(none)")
        now_game = summary != "(none)"
        S.STEAM_APP_SUMMARY = summary
        log("STEAM PROC: running Steam games: " + summary)
        # (Слой 3) game session boundary -> [GAME-START]/[GAME-STOP] + full cap
        if now_game and not had_game:
            _game_boundary(summary, start=True)
        elif had_game and not now_game:
            _game_boundary(summary, start=False)


def _steam_config_marker(focus):
    """(Слой 2) Unified [STEAM-CONFIG] active verdict from a focus label:
    shell 769 vs Big Picture 413080 vs a GAME AppID — logged at the moment the
    focused AppID changes."""
    m = re.search(r"\bAppID (\d+)", focus or "")
    appid = m.group(1) if m else "?"
    if "GAME window" in (focus or ""):
        active = f"GAME:{appid}"
    elif appid == "769":
        active = "769"
    elif appid == "413080":
        active = "413080"
    else:
        active = appid
    log(f"[STEAM-CONFIG] active={active} ({focus})")


# G1 (todo 52b): the deck identity (Serial 28de-... / ProductID 0x12F0) appears
# in the controller_ui.txt ATTRIBUTE BLOCK that FOLLOWS a "Controller N
# connected" line — never on the same line. So the old same-line filter never
# fired. We now run a per-controller state machine over the tail-diffed lines:
#   "Controller N connected"  -> N pending identity
#   "Controller N disconnected" -> if N was the deck -> [STEAM-RELEASE]
#   attribute block (Serial 28de-... / deck ProductID) -> N IS the deck; if N
#     was pending -> [STEAM-GRAB] (fresh) or [STEAM-REGRAB] (re-grab after a
#     release). Persistent state lives in S.STEAM_CACHE["__ctl_state__"].
_CTL_STATE_KEY = "__ctl_state__"
# deck ProductIDs (observable identity of the virtual Steam Deck controller)
_DECK_PIDS = (0x12F0, 0x12FB, 0x1205)


def _ctl_state():
    st = S.STEAM_CACHE.get(_CTL_STATE_KEY)
    if st is None:
        st = {"connecting": set(), "deck": set(), "connected": set(),
              "released": False}
        S.STEAM_CACHE[_CTL_STATE_KEY] = st
    return st


def _ctl_index(line):
    """Controller index from a 'Controller N ...' line, or None."""
    m = re.search(r"Controller\s+(\d+)", line)
    return int(m.group(1)) if m else None


def _is_deck_identity(serial, product):
    """True when an attribute block identifies the virtual Steam Deck
    controller (Serial 28de-... or a deck ProductID)."""
    if serial and "28de" in serial.lower():
        return True
    if product:
        try:
            return int(product, 16) in _DECK_PIDS
        except ValueError:
            try:
                return int(product) in _DECK_PIDS
            except ValueError:
                pass
    return False


def _steam_controller_verdicts(newlines):
    """(Слой 2) [STEAM-GRAB]/[STEAM-RELEASE]/[STEAM-REGRAB] from
    controller_ui.txt connect/disconnect/attributes events — the Steam-eye view
    of the deck-uhid lifecycle. G1: fires from REAL Steam-side evidence by
    correlating the connect/disconnect lines with the deck identity in the
    following attribute block (Serial 28de-... / deck ProductID)."""
    st = _ctl_state()
    for line in newlines:
        low = line.lower()
        idx = _ctl_index(line)
        # -- attribute block: Serial / ProductID identify the controller -------
        if "serial:" in low:
            # Serial is the authoritative deck marker (28de-...); apply it to
            # the most recent pending controller immediately.
            serial = line.split(":", 1)[1].strip()
            _apply_deck_identity(st, idx, serial, None)
            continue
        if "productid:" in low:
            # remember the ProductID so a Serial-less block can still identify
            product = line.split(":", 1)[1].strip()
            _apply_deck_identity(st, idx, None, product)
            continue
        if "connected" in low and "configuring" in low:
            # connect event: controller N is pending identity
            if idx is not None:
                st["connecting"].add(idx)
            continue
        if "disconnected" in low:
            if idx is not None and idx in st["deck"]:
                log(f"[STEAM-RELEASE] {line.strip()}")
                st["released"] = True
                _enter_full("steam-release")
                snapshot_state("steam-release")
            if idx is not None:
                st["deck"].discard(idx)
                st["connected"].discard(idx)
                st["connecting"].discard(idx)
            continue


def _apply_deck_identity(st, idx, serial, product):
    """When an attribute block identifies the deck, mark the pending controller
    as the deck and emit [STEAM-GRAB] (fresh) or [STEAM-REGRAB] (re-grab after
    a release)."""
    if not _is_deck_identity(serial, product):
        return
    # the deck identity applies to the most recent pending controller
    if idx is None:
        if not st["connecting"]:
            return
        idx = max(st["connecting"])
    if idx in st["deck"]:
        return
    st["deck"].add(idx)
    st["connected"].add(idx)
    st["connecting"].discard(idx)
    if st["released"]:
        st["released"] = False
        log(f"[STEAM-REGRAB] Controller {idx} re-grabbed "
            f"(serial={serial or '?'} product={product or '?'})")
        mark_regrab(time.monotonic(), "steam-regrab")
        _enter_full("steam-re-grab")
        snapshot_state("steam-re-grab")
    else:
        log(f"[STEAM-GRAB] Controller {idx} grabbed "
            f"(serial={serial or '?'} product={product or '?'})")
        mark_regrab(time.monotonic(), "steam-grab")
        _enter_full("steam-grab")
        snapshot_state("steam-grab")


def _window_title():
    """(R5) Best-effort focused window title (the game window the user is in).
    Uses `xdotool` if present; returns '' when unavailable so the logger never
    depends on an extra package. No new deps are added."""
    from .logcore import run_cmd  # local import keeps module deps flat
    out = run_cmd(["xdotool", "getactivewindow", "getwindowname"], timeout=3)
    return out or ""


def _proc_detail():
    """(R5) Compact per-process detail of every running Steam game:
    pid/appid/name/exec, from S.STEAM_PROC_SET (pid -> (appid, name, cmd))."""
    parts = []
    for pid in sorted(S.STEAM_PROC_SET):
        appid, name, cmd = S.STEAM_PROC_SET[pid]
        parts.append(f"pid={pid} appid={appid} name={name} exec={cmd or '-'}")
    return "; ".join(parts) or "(none)"


def steam_state_verdict():
    """(Слой 2) Periodic [STEAM-STATE] — Steam-layer liveness: focused AppID,
    running game procs (pid/appid/exec), whether a game is up, whether deck was
    keep-alived. (R5) enriched with per-proc metadata + window title."""
    win = _window_title()
    log(f"[STEAM-STATE] focus={S.STEAM_UI_FOCUS or 'none'} "
        f"procs={S.STEAM_APP_SUMMARY or '(none)'} "
        f"game={'yes' if S.GAME_FOCUSED else 'no'} "
        f"deck_keepalive={'yes' if S.DECK_KEEPALIVE else 'no'} "
        f"window={win or '-'} "
        f"proc_detail=[{_proc_detail()}]")
