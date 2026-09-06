# -*- coding: utf-8 -*-
"""
ipgyro.mode — REAL Steam UI / session mode detection (todo 52r, LOG_VERSION v3.12,
FIX-A: the v3.11 "gaming-mode" lie).

WHY THIS MODULE EXISTS
----------------------
The v3.11 event table claimed "gaming-mode" for the whole session while the user
was in a DESKTOP session the entire time (KDE Plasma), including while ESO ran
(launched from the desktop). The v3.11 mode was derived from (a) the TRANSPORT
deck-output roles present (_deck_mode: a uhid "gaming" deck exists whenever a
Steam-Input game runs under the desktop, so a desktop+game looked like GAMING)
and (b) the Steam-Input focused-window verdict (record_mode_verdict: a focused
GAME AppID = 'game'). Both answer the WRONG question: they describe which virtual
deck Steam created / which window is focused, NOT which session the user is in.

The ONE question that matters for a session-mode tag is: "is a DESKTOP session
present, or is this a GAMING session (Steam Big Picture / gamescope with NO
desktop shell)?". A running game does NOT switch the mode — launching ESO from
the desktop stays DESKTOP.

DETECTION (host-side, read-only, additive; every да/нет fact below is backed by
a real host signal — never an assumption):
  DESKTOP (any positive evidence => desktop, listed in the [MODE ... via ...] tag):
    D1 a running desktop compositor/shell process:
       gnome-shell / plasmashell / kwin_wayland / kwin_wayland_wrapper /
       mutter / startplasma-way / startplasma-x11 / gnome-session /
       xfce4-session / cinnamon-session / mate-session / kded6 ...
       (a bare compositor under gamescope never spawns these shells, so a
       gamescope-only gaming session stays clear of D1);
    D2 XDG_CURRENT_DESKTOP set (non-empty) on a live graphical session leader
       (the session whose loginctl Type is x11|wayland and Class=user);
    D3 a loginctl session with Class=user and Type in (x11,wayland) on a seat
       (seat0), i.e. a real graphical desktop login.
  GAMING (NEVER claimed while any DESKTOP evidence is present):
    G1 a gamescope process running (SteamOS gaming session / nested gamescope);
    G2 a Steam process whose cmdline/env carries the gamepad-UI flag
       (-gamepadui / -steamdeck / SteamDeck=1) with NO desktop shell.
  Otherwise -> 'unknown' with the reasons listed (signal conflict/absence is
  reported, never guessed).

The verdict is returned as (mode, sources, detail):
  mode     'desktop' | 'gaming' | 'unknown'
  sources  list of human evidence strings (e.g. ['XDG_CURRENT_DESKTOP=KDE',
           'session seat0 type=wayland', 'DE process plasmashell'])
  detail   the reason for the verdict / the conflicting-or-absent reasons.

This module imports ONLY conf + logcore (cycle-safe): obs/steam/app import it,
it never imports them.
"""

import os
import time

from . import state as S
from .logcore import log, run_cmd

# ---------------------------------------------------------------------------
# Evidence vocabularies
# ---------------------------------------------------------------------------

# (D1) Desktop shell/compositor processes that only exist in a desktop session.
_DE_COMMS = {
    "gnome-shell", "gnome-shell-x11", "gnome-session", "gnome-session-b",
    "plasmashell", "kwin_wayland", "kwin_wayland_wr", "kwin_x11",
    "startplasma-way", "startplasma-x11", "mutter", "mutter-x11",
    "xfce4-session", "cinnamon-session", "cinnamon", "mate-session",
    "budgie-daemon", "lxqt-session", "enlightenment", "xfwm4", "kded6",
    "plasma-session", "cosmic-comp", "cosmic-session",
}
# long process names are truncated to 15 chars in /proc/comm; these are the
# <=15-char prefixes already covered above (kwin_wayland -> kwin_wayland;
# kwin_wayland_wrapper -> kwin_wayland_wr; startplasma-way is 14).

# (G1) gamescope = the SteamOS/nested gaming compositor.
_GAMESCOPE_COMMS = {"gamescope", "gamescope-wl"}

# (G2) Steam gamepad-UI markers in a Steam process cmdline/env.
_STEAM_GPUI_CMDLINE = ("-gamepadui", "-steamdeck", "--gamepadui")
_STEAM_GPUI_ENV = ("STEAM_GAMEPADUI=", "SteamDeck=", "STEAM_DECK=")


# ---------------------------------------------------------------------------
# Host signal readers (pure-ish; never mutate system state, only read)
# ---------------------------------------------------------------------------

def _proc_comms():
    """{pid -> comm} of every process, best-effort (reads /proc/<pid>/comm)."""
    out = {}
    try:
        entries = os.listdir("/proc")
    except Exception:
        return out
    for p in entries:
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/comm") as f:
                out[int(p)] = f.read().strip() or ""
        except Exception:
            continue
    return out


def _read_environ(pid):
    """Decoded environ dict of one pid ('' values on any failure)."""
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


def _loginctl_sessions():
    """List of graphical user sessions: {id,type,class,seat,active,leader}.
    Best-effort via loginctl (never raises)."""
    sessions = []
    out = run_cmd(["loginctl", "list-sessions", "--no-legend"], timeout=5)
    if not out:
        return sessions
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        show = run_cmd(["loginctl", "show-session", sid, "-p", "Type",
                        "-p", "Class", "-p", "Seat", "-p", "Active",
                        "-p", "Leader"], timeout=5)
        d = {"id": sid, "type": "", "class": "", "seat": "", "active": "",
             "leader": ""}
        for kv in (show or "").splitlines():
            k, _, v = kv.partition("=")
            if k in d:
                d[k] = v
        sessions.append(d)
    return sessions


# ---------------------------------------------------------------------------
# Evidence collectors
# ---------------------------------------------------------------------------

def desktop_evidence():
    """Return (present: bool, sources: [str], reasons: [str]).
    present = a DESKTOP session is detected (any of D1/D2/D3)."""
    sources = []
    reasons = []
    comms = _proc_comms()
    # -- D1: desktop shell / compositor processes -----------------------------
    de_found = []
    for pid, comm in comms.items():
        if comm in _DE_COMMS:
            de_found.append(comm)
    if de_found:
        # dedupe + keep the most meaningful shells first
        seen = []
        for c in de_found:
            if c not in seen:
                seen.append(c)
        sources.append("DE process " + ",".join(seen[:4]))
    else:
        reasons.append("no desktop shell process")

    # -- D3: graphical user session on a seat (plus leader env for D2) --------
    active_graph = None
    for s in _loginctl_sessions():
        if s.get("class") != "user":
            continue
        if s.get("type") in ("x11", "wayland") and s.get("seat"):
            src = f"session {s['id']} type={s['type']} on {s['seat']}"
            if src not in sources:
                sources.append(src)
            if s.get("active") == "yes":
                active_graph = s
        elif s.get("seat"):
            reasons.append(
                f"session {s.get('id')} class=user type={s.get('type') or '?'} "
                f"(not a graphical type)")
    if not any("session " in s for s in sources):
        reasons.append("no graphical user session on a seat")

    # -- D2: XDG_CURRENT_DESKTOP from the active graphical session leader ------
    if active_graph:
        leader = active_graph.get("leader") or ""
        if leader.isdigit():
            env = _read_environ(int(leader))
            xdg = env.get("XDG_CURRENT_DESKTOP", "").strip()
            if xdg:
                sources.append(f"XDG_CURRENT_DESKTOP={xdg}")
    # fallback: any desktop-y value already found in a running session (root
    # service can read every session leader; unprivileged run degrades to D1/D3)
    if not any(s.startswith("XDG_CURRENT_DESKTOP=") for s in sources):
        reasons.append("XDG_CURRENT_DESKTOP unreadable/unset")

    return bool(sources), sources, reasons


def gaming_evidence():
    """Return (present: bool, sources: [str]). Gamescope/gamepad-UI WITHOUT a
    desktop shell. Callers must only trust this when desktop_evidence() is empty
    (gaming is NEVER hard-claimed while a desktop session is present)."""
    sources = []
    comms = _proc_comms()
    if any(comm in _GAMESCOPE_COMMS for comm in comms.values()):
        sources.append("gamescope process (no-desktop gaming compositor)")
    # -- Steam with the gamepad-UI flag ----------------------------------------
    for pid, comm in comms.items():
        if comm not in ("steam", "steamwebhelper"):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(
                    "utf-8", "replace")
        except Exception:
            continue
        if any(fl in cmdline for fl in _STEAM_GPUI_CMDLINE):
            sources.append(
                f"steam pid={pid} gamepad-UI ({','.join(_STEAM_GPUI_CMDLINE)})")
            break
        env = _read_environ(pid)
        if any(env.get(k.split("=")[0], "") for k in _STEAM_GPUI_ENV):
            sources.append(f"steam pid={pid} SteamDeck env (gamepad-UI)")
            break
    return bool(sources), sources


def classify():
    """The REAL session-mode verdict: (mode, sources, detail).

    mode     'desktop' | 'gaming' | 'unknown'
    sources  evidence strings shown in the [MODE ... via ...] tag
    detail   human reason (or the conflicting/absent reasons when unknown).

    Deterministic order of preference:
      1. any DESKTOP evidence  -> desktop (a running game NEVER overrides this);
      2. no desktop + GAMING evidence -> gaming;
      3. otherwise -> unknown (reasons listed; never guessed).
    """
    desk, desk_src, desk_reasons = desktop_evidence()
    if desk:
        return "desktop", desk_src, (
            "desktop session present (" + ", ".join(desk_src) + ")")
    game, game_src = gaming_evidence()
    if game and not desk:
        return "gaming", game_src, (
            "no-desktop Big Picture/gamescope session detected ("
            + ", ".join(game_src) + ")")
    reasons = desk_reasons
    if not game:
        reasons = reasons + ["no gamescope/gamepad-UI process"]
    why = "; ".join(reasons) if reasons else "no session signals at all"
    return "unknown", [], "no desktop and no gaming session evidence (" + why + ")"


# ---------------------------------------------------------------------------
# Shared live-verdict state helpers (single writer: obs.mode_scan)
# ---------------------------------------------------------------------------

def word(mode):
    """Human word for the [MODE ... via ...] tag / SNAPSHOT mode= field."""
    return mode if mode in ("desktop", "gaming", "unknown") else "unknown"


def live():
    """The last cached live verdict word (S.MODE), defaulting to a fresh
    classify() when never scanned yet."""
    if S.MODE is not None:
        return S.MODE
    m, _src, _det = classify()
    return m


def scan(now, force=False):
    """Recompute the real session-mode verdict and cache it on S. Logs
    [MODE <word> via <sources>] ONLY on a change (or on the first scan).
    force=True always re-classifies (used right before a [DETACH]/[DOCK]
    mode= tag so the tag is never a stale periodic verdict). Returns the word."""
    m, srcs, det = classify()
    changed = (m != S.MODE)
    S.MODE = m
    S.MODE_SRC = srcs
    S.MODE_DETAIL = det
    if changed and (force or now - S.MODE_LAST_LOG >= 1.0):
        S.MODE_LAST_LOG = now
        via = ", ".join(srcs) if srcs else ("reasons: " + det)
        log(f"[MODE] {word(m)} via {via}")
    return m
