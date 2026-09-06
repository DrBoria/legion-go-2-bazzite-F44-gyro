# -*- coding: utf-8 -*-
"""
ipgyro.flow — v3.13 (todo 52t) PER-HOP FLOW PROOF.

THE USER DEMAND (52t, verbatim intent)
--------------------------------------
v3.12 diagnosed Wake#2 as «НЕТ (не доказуемо — game focus lost, not gyro
death)». The user rejects that cell: «это нужно корректно отмечать не как не
доказуемо, а блять доказывать, просто ты должен трекать на каком месте логи
теряются». So the logger must PROVE WHERE in the chain the gyro (and any data
type) is lost, hop by hop, with evidence on every line — and NEVER report
«не доказуемо» when a game is running and detectable.

HOP MODEL (per data type; the FIRST hop where data is absent is the loss point)
-------------------------------------------------------------------------------
  H0 PHYSICAL SOURCE  gyro: while handles are attached the CENTER body IMU
                      travels in the XInput right_gyro slot (bytes 54-59,
                      values != 0 when moving; left_gyro is dead); when the
                      right handle detaches it is its own 17ef:61ed device
                      (gyro_carrier = right-handle). Buttons/sticks: the
                      physical controller source is present.
  H1 DECK->STEAM      deck-uhid 28de:12f0 frames carry the motion/actions to
                      Steam. Proven by the deck motion counter (mot/s) +
                      button/stick change counter (act/s): mot>0 while
                      physically moving = the deck IS emitting gyro to Steam.
  H2 STEAM ROUTING    which Steam Input config Steam applies to the deck:
                      [STEAM-CONFIG] active appid = GAME:<appid> vs DESKTOP
                      413080. KEY LOGIC: if a game process is running
                      (detectable: SteamAppId in /proc environ/cmdline of the
                      game/Proton tree, game reader present) but Steam's
                      active controller config/focus != that game appid,
                      Steam is NOT routing the deck through the game's config
                      -> input (incl. gyro) does NOT go to the game. That is
                      a PROVEN loss with reason — never «не доказуемо».
  H3 PAD->GAME        Steam writes the config-processed result to its OWN
                      virtual pad 28de:11ff; gyro is provable there ONLY as
                      gyro-axis ABS_RX/ABS_RY pad events while the real game
                      holds the pad fd ([GYRO->PAD-AXIS]); buttons/sticks as
                      pad_key/s + pad_abs/s ([PAD->GAME]).

Every verdict function below is PURE: it takes a flow-snapshot dict of the
observable inputs and returns per-hop verdicts (ok|lost|none + reason), so the
SAME logic drives (a) the live logger and (b) the replay-sanity harness that
re-runs this logic against the recorded v3.12 Wake#2 window.

Shared mutable state is accessed ONLY as S.<name>. No module-level import of
obs (obs lazily imports this module), so module-load order stays cycle-free.
"""

import time

from . import state as S
from .conf import FLOW_CHANGE_MIN, FLOW_MIN_LOG, FOCUS_LOST_HB, WAKE_FLOW_S
from .logcore import log

OK, LOST, NONE = "ok", "lost", "none"

# (dtype, marker token, human noun) — sticks share the buttons physical path.
TYPE_ORDER = (("gyro", "GYRO", "gyro"),
              ("buttons", "BTNS", "buttons"),
              ("sticks", "STX", "sticks/triggers"))


# ---------------------------------------------------------------------------
# Hop record helpers
# ---------------------------------------------------------------------------

def _hop(idx, verdict, short, detail):
    """One hop record: index, H-token, verdict (ok|lost|none), short reason
    (goes into the Hn=ok(...) chain) and the full detail (the quoted evidence
    after «lost at Hn: ...»)."""
    return {"idx": idx, "hop": "H%d" % idx, "verdict": verdict,
            "short": short, "detail": detail}


def first_lost(hops):
    """'H2' for the FIRST hop whose verdict is 'lost', else None."""
    for h in hops:
        if h["verdict"] == LOST:
            return h["hop"]
    return None


def first_non_ok(hops):
    """'H2' for the FIRST hop whose verdict is not 'ok' (lost OR none) — used
    to say where an idle/no-data stream first stops, honestly."""
    for h in hops:
        if h["verdict"] != OK:
            return h["hop"]
    return None


def _all_idle_verdicts(hops):
    """True when every hop is 'none' (no live data anywhere to trace)."""
    return all(h["verdict"] == NONE for h in hops)


# ---------------------------------------------------------------------------
# H2 STEAM ROUTING (shared by every data type)
# ---------------------------------------------------------------------------

def routing(inp):
    """H2 routing verdict. A running game (SteamAppId in /proc) with Steam's
    active controller config/focus != that game appid is a PROVEN routing loss
    (Steam applies the desktop/other config, so input does not reach the game).
    Returns {verdict, short, detail, word}."""
    appids = [str(a) for a in (inp.get("game_appids") or [])]
    running = bool(inp.get("game_running")) or bool(appids)
    fk = inp.get("focus_kind")
    fa = inp.get("focus_appid")
    fa_s = str(fa) if fa is not None else "none"
    if not running:
        return {"verdict": NONE, "word": "none",
                "short": "no game",
                "detail": ("no running game detected — Steam routing is N/A "
                           "(H2 none: nothing to route to the game)")}
    if fk == "GAME" and (fa in appids or not appids):
        return {"verdict": OK, "word": "game",
                "short": "config=GAME:%s" % fa_s,
                "detail": ("Steam active controller config/focus = GAME:%s — "
                           "deck IS routed through the running game's config "
                           "(%s)") % (fa_s, ",".join(appids) or "?")}
    return {"verdict": LOST, "word": fa_s,
            "short": "config=%s vs game=%s" % (fa_s, ",".join(appids) or "?"),
            "detail": ("Steam active controller config/focus = %s (kind=%s) "
                       "while game %s is running -> Steam is NOT routing the "
                       "deck through the game's config; input (incl. gyro) "
                       "does NOT go to the game") % (
                           fa_s, fk or "?", ",".join(appids) or "?")}


# ---------------------------------------------------------------------------
# GYRO per-hop verdicts (H0 physical -> H1 deck -> H2 routing -> H3 pad)
# ---------------------------------------------------------------------------

def gyro_hops(inp, rt=None):
    rt = rt if rt is not None else routing(inp)
    carrier = inp.get("carrier", "none")
    iio_moving = inp.get("iio_moving", False)
    deck_rd = inp.get("deck_rd", 0)
    deck_mot = inp.get("deck_mot", 0)
    game_deck = inp.get("game_deck", False)
    hops = [None, None, None, None]

    # -- H0 physical source ------------------------------------------------
    if carrier in ("center", "right-handle"):
        moving = iio_moving or deck_mot > 0
        if not moving:
            hops[0] = _hop(0, NONE, "carrier=%s idle" % carrier,
                           ("physical gyro source present (gyro_carrier=%s) "
                            "but NOT physically moving this second — nothing "
                            "to forward (idle)") % carrier)
        else:
            hops[0] = _hop(0, OK, "rd right_gyro!=0 carrier=%s" % carrier,
                           ("physical gyro LIVE (gyro_carrier=%s; center-body "
                            "IMU in the XInput right_gyro slot / right-handle "
                            "17ef:61ed), moving now: deck mot=%s/s, "
                            "iio_moving=%s") % (carrier, deck_mot, iio_moving))
    else:
        hops[0] = _hop(0, LOST, "no phys gyro",
                       "NO physical gyro source (gyro_carrier=none, no "
                       "physical XInput/IIO decode) — H0 lost")

    # -- H1 deck -> Steam ---------------------------------------------------
    # The "lost" verdicts here are only meaningful when Steam is actually
    # routing to a game (rt verdict == OK). During idle desktop with no game
    # (rt == none) a deck not forwarding a tiny IIO noise blip is NOT a loss —
    # it is idle (H1=none), so the chain never cries "lost" while no game runs.
    routing_game = rt["verdict"] == OK
    if deck_rd > 0:
        if deck_mot > 0:
            hops[1] = _hop(1, OK, "%s mot/s %s rd/s" % (deck_mot, deck_rd),
                           ("deck-uhid delivering %s rd/s with %s IMU-mot/s — "
                            "the deck IS emitting gyro to Steam (H1 ok)")
                           % (deck_rd, deck_mot))
        elif iio_moving and routing_game:
            hops[1] = _hop(1, LOST, "deck %s rd/s mot=0" % deck_rd,
                           ("deck delivering %s rd/s but 0 IMU-mot/s while the "
                            "physical gyro is moving (iio_moving=True) and the "
                            "game config is active — InputPlumber/deck NOT "
                            "forwarding gyro to Steam (H1)") % deck_rd)
        else:
            hops[1] = _hop(1, NONE, "deck %s rd/s mot=0" % deck_rd,
                           ("deck delivering %s rd/s but no physical motion to "
                            "forward (idle — no gyro to carry)") % deck_rd)
    elif game_deck and routing_game:
        hops[1] = _hop(1, LOST, "deck 0 rd/s",
                       "gaming deck present but 0 rd/s while the game config "
                       "is active — deck stream to Steam STOPPED (H1)")
    else:
        hops[1] = _hop(1, NONE, "no deck frames",
                       "no gaming deck / no deck frames on hop 1 (H1 idle)")

    hops[2] = _hop(2, rt["verdict"], rt["short"], rt["detail"])
    hops[3] = _gyro_h3(inp, rt, deck_mot)
    return hops


def _gyro_h3(inp, rt, deck_mot):
    """H3 pad->game for gyro: gyro-axis ABS_RX/ABS_RY pad events while the game
    holds the pad fd. If routing is already lost at H2 the pad hop is not
    applicable (H3=none with the H2 proof) — the loss is AT H2, never vague."""
    if rt["verdict"] == NONE:
        return _hop(3, NONE, "no game target",
                    "no game routed (H2 none) — nothing to deliver to "
                    "(H3 N/A)")
    if rt["verdict"] == LOST:
        return _hop(3, NONE, "routing lost @H2",
                    "routing already lost at H2 (Steam config focus != the "
                    "running game) — the pad hop is not applicable: gyro never "
                    "leaves Steam for the game")
    pad_node = inp.get("pad_node", False)
    pad_gyro = inp.get("pad_gyro", 0)
    pad_ev = inp.get("pad_ev", 0)
    game_reader = inp.get("game_reader", False)
    if not pad_node:
        return _hop(3, LOST, "no pad 28de:11ff",
                    "game config active but NO Steam virtual pad node "
                    "(28de:11ff) present — Steam not exposing the game pad "
                    "(H3)")
    if game_reader:
        if pad_gyro > 0:
            return _hop(3, OK, "%s gyro-axis/s" % pad_gyro,
                        ("%s gyro-axis ABS_RX/ABS_RY events/s on pad read by "
                         "the game — gyro PROVABLY reached the game (H3 ok)")
                        % pad_gyro)
        if deck_mot > 0:
            return _hop(3, LOST, "0 gyro-axis/s mot!=0",
                        ("game config active + game reader present + deck "
                         "mot=%s/s but 0 gyro-axis pad events — Steam is NOT "
                         "writing gyro to the pad (H3)") % deck_mot)
        return _hop(3, NONE, "reader idle",
                    "game reader present but no physical motion to forward")
    if pad_ev > 0:
        return _hop(3, LOST, "no game reader",
                    ("pad events %s/s but NO game reader holds the pad fd — "
                     "data reaches Steam's pad but no game consumes it (H3)")
                    % pad_ev)
    return _hop(3, NONE, "no reader yet",
                "game focused but no game reader on the pad fd yet")


# ---------------------------------------------------------------------------
# BUTTONS / STICKS per-hop verdicts (shared physical path)
# ---------------------------------------------------------------------------

def btn_hops(inp, rt, sticks=False):
    """Buttons/sticks hops. The pad counter probed is pad_key (buttons) or
    pad_stick (sticks/triggers, pad_abs non-gyro). deck_act is the deck
    button/stick/trigger CHANGE counter (hop-1 forwarding evidence)."""
    rt = rt if rt is not None else routing(inp)
    noun = "sticks" if sticks else "buttons"
    deck_act = inp.get("deck_act", 0)
    phys_present = inp.get("phys_present", False)
    game_deck = inp.get("game_deck", False)
    pad_x = inp.get("pad_stick" if sticks else "pad_key", 0)
    pad_ev = inp.get("pad_ev", 0)
    pad_node = inp.get("pad_node", False)
    game_reader = inp.get("game_reader", False)
    hops = [None, None, None, None]

    # -- H0 physical source --------------------------------------------------
    if phys_present:
        hops[0] = _hop(0, OK if (deck_act > 0 or pad_x > 0) else NONE,
                       "phys present",
                       "physical controller source present — %s presses CAN "
                       "flow" % noun)
    elif game_deck:
        hops[0] = _hop(0, NONE, "deck-only",
                       "no physical source (deck-only stream) — %s source is "
                       "the deck itself" % noun)
    else:
        hops[0] = _hop(0, NONE, "controller detached",
                       "controller detached / no physical source and no deck — "
                       "no %s source on H0" % noun)

    # -- H1 deck -> Steam (buttons/sticks forwarded as deck actions) ---------
    if deck_act > 0 or pad_x > 0:
        hops[1] = _hop(1, OK, "deck %s act/s" % deck_act,
                       ("deck-uhid forwarding %s act/s (%s changes) to Steam "
                        "— H1 ok") % (deck_act, noun))
    elif (phys_present or game_deck) and rt["verdict"] == OK:
        # game config routes the deck but no deck action reached Steam while a
        # controller is present — the deck act=0 case the user listed for H1.
        # Only call it LOST when input is expected (game focused) AND the pad
        # is NOT delivering this type (else the press would show downstream).
        if pad_ev == 0:
            hops[1] = _hop(1, LOST, "deck act=0 game-focused",
                           ("game config active + controller present but deck "
                            "act=0 (no %s change forwarded to Steam) while the "
                            "pad receives nothing — H1 lost") % noun)
        else:
            hops[1] = _hop(1, NONE, "deck act=0",
                           "deck act=0 this window (no %s press to forward)" % noun)
    else:
        hops[1] = _hop(1, NONE, "no presses",
                       "no %s press observed this window — nothing on hop 1 to "
                       "trace" % noun)

    hops[2] = _hop(2, rt["verdict"], rt["short"], rt["detail"])

    # -- H3 pad -> game ------------------------------------------------------
    if rt["verdict"] == NONE:
        hops[3] = _hop(3, NONE, "no game target",
                       "no game routed (H2 none) — %s N/A at H3" % noun)
    elif rt["verdict"] == LOST:
        hops[3] = _hop(3, NONE, "routing lost @H2",
                       "routing already lost at H2 — %s never leave Steam for "
                       "the game" % noun)
    elif not pad_node:
        hops[3] = _hop(3, LOST, "no pad 28de:11ff",
                       "game config active but NO Steam virtual pad node — "
                       "Steam not exposing the game pad (H3)")
    elif game_reader:
        if pad_x > 0:
            hops[3] = _hop(3, OK, "pad_%s/s=%s" %
                           ("key" if not sticks else "abs", pad_x),
                           ("%s pad events/s on the pad read by the game — %s "
                            "PROVABLY reached the game (H3 ok)")
                           % (pad_x, noun))
        elif deck_act > 0 or pad_ev > 0:
            hops[3] = _hop(3, LOST, "pad_%s=0 game+reader" %
                           ("key" if not sticks else "abs"),
                           ("game config active + game reader present + deck "
                            "act=%s/s but 0 %s pad events — Steam NOT writing "
                            "%s to the pad (H3)") % (deck_act, noun, noun))
        else:
            hops[3] = _hop(3, NONE, "no press",
                           "game reader present but no %s press this window" % noun)
    elif pad_ev > 0:
        hops[3] = _hop(3, LOST, "no game reader",
                       ("pad events %s/s but NO game reader holds the pad fd — "
                        "%s reach Steam's pad but no game consumes them (H3)")
                       % (pad_ev, noun))
    else:
        hops[3] = _hop(3, NONE, "no reader yet",
                       "game focused but no game reader on the pad fd yet")
    return hops


# ---------------------------------------------------------------------------
# compute / compact (used by live logger AND snapshot + replay harness)
# ---------------------------------------------------------------------------

def type_hops(dtype, inp):
    rt = routing(inp)
    if dtype == "gyro":
        return gyro_hops(inp, rt)
    if dtype == "buttons":
        return btn_hops(inp, rt, sticks=False)
    return btn_hops(inp, rt, sticks=True)   # sticks


def compute(inp):
    """Per-type hop lists for a flow snapshot: {'gyro': [H0..H3], 'buttons':
    [...], 'sticks': [...]}."""
    return {dtype: type_hops(dtype, inp) for dtype, _t, _n in TYPE_ORDER}


def _type_compact(hops):
    """Compact per-type chain, e.g. 'H0ok,H1ok,H2game,H3ok' (H2 shows the
    routing target word 'game' when Steam routes to the game)."""
    parts = []
    for h in hops:
        if h["idx"] == 2 and h["verdict"] == OK:
            parts.append("H2game")
        else:
            parts.append("%s%s" % (h["hop"], h["verdict"]))
    return ",".join(parts)


def compact(inp, per=None):
    """One compact flow line for the SNAPSHOT/FLOW markers, e.g.
    'gyro:H0ok,H1ok,H2game,H3ok|btns:H0ok,H1ok,H2game,H3ok|stx:...'."""
    per = per if per is not None else compute(inp)
    toks = {"gyro": "gyro", "buttons": "btns", "sticks": "stx"}
    return "|".join("%s:%s" % (toks[d], _type_compact(per[d]))
                    for d, _t, _n in TYPE_ORDER)


def _hops_chain(hops):
    return " ".join("H%d=%s(%s)" % (h["idx"], h["verdict"], h["short"])
                    for h in hops)


def _terminal(dtype, hops, first):
    """The human verdict after the per-hop chain — always names the FIRST lost
    hop with quoted evidence, or states honestly where the stream first idles
    when nothing is lost. Never says «не доказуемо»."""
    if first:
        h = hops[int(first[1])]
        return ("%s lost at %s: %s" % (dtype, first, h["detail"]))
    if hops[3]["verdict"] == OK:
        return ("%s flow COMPLETE to the game (all hops ok, delivery proven)" %
                dtype)
    idle = first_non_ok(hops)
    if _all_idle_verdicts(hops):
        return ("%s: no live %s data this instant (idle / no game) — nothing "
                "is lost; first idle hop %s: %s"
                ) % (dtype, dtype, idle, hops[int(idle[1])]["detail"])
    return ("%s: no LOST hop; data first absent at %s (%s: %s) — downstream "
            "hops not applicable until this resumes"
            ) % (dtype, idle, idle, hops[int(idle[1])]["detail"])


def type_marker(dtype, hops):
    """[GYRO-FLOW ...] / [BTNS-FLOW ...] / [STX-FLOW ...] verbose marker."""
    token = dict((d, t) for d, t, _n in TYPE_ORDER)[dtype]
    noun = dict((d, n) for d, _t, n in TYPE_ORDER)[dtype]
    first = first_lost(hops)
    return "[%s-FLOW %s] — %s" % (token, _hops_chain(hops),
                                  _terminal(noun, hops, first))


def _type_key(hops):
    """Change-detection key: the per-hop verdicts (a hop ok->lost/none or the
    reverse changes the key)."""
    return "|".join(h["verdict"] for h in hops)


# ---------------------------------------------------------------------------
# [STEAM-FOCUS-LOST-GAME] / [STEAM-FOCUS-DROP-AFTER-WAKE] (pure builders)
# ---------------------------------------------------------------------------

def focus_lost_condition(inp):
    """True when a game is running but Steam's active controller config focus
    is NOT that game (the routing hop H2 is lost)."""
    appids = [str(a) for a in (inp.get("game_appids") or [])]
    if not (inp.get("game_running") or bool(appids)):
        return False
    fa = inp.get("focus_appid")
    if fa in appids:
        return False
    if inp.get("focus_kind") == "GAME" and fa is not None and fa not in appids:
        return True     # focused on a DIFFERENT game/window than the running one
    return True         # focus is desktop (413080/769/OTHER) or unknown


def focus_lost_line(inp):
    """[STEAM-FOCUS-LOST-GAME] evidence line (focus appid + game appid + game
    pid/name + the config focus label)."""
    appids = [str(a) for a in (inp.get("game_appids") or [])]
    pids = inp.get("game_pids") or []     # list of "pid:name" strings
    fa = inp.get("focus_appid")
    fa_s = str(fa) if fa is not None else "none"
    pid_s = "; ".join(pids) if pids else "(none detected)"
    return ("[STEAM-FOCUS-LOST-GAME] focus_appid=%s focus_kind=%s "
            "game_appid=%s game_pids=[%s] evidence=%s — a running game is "
            "detectable but Steam's active controller config/focus is NOT the "
            "game -> Steam is NOT routing deck input (incl. gyro) to the game "
            "(routing hop H2 lost)" % (
                fa_s, inp.get("focus_kind") or "?",
                ",".join(appids) or "?", pid_s,
                inp.get("focus_label") or "none"))


def focus_drop_line(inp, wake_mono, delay):
    """[STEAM-FOCUS-DROP-AFTER-WAKE] — a game was Steam-focused BEFORE sleep and
    after wake the config focus is NOT the game appid. Attributes the loss to
    the wake + routing, never to a detach, never vague."""
    pre_a = inp.get("pre_focus_appid")
    post_a = inp.get("focus_appid")
    return ("[STEAM-FOCUS-DROP-AFTER-WAKE] focus:%s->%s at wake (+%.1fs) — "
            "game %s was Steam-focused before sleep, Steam config focus=%s "
            "(kind=%s) after wake -> Steam NOT routing the deck through the "
            "game config; loss attributed to the wake + routing hop (H2), NOT "
            "to detach" % (
                pre_a if pre_a is not None else "?",
                post_a if post_a is not None else "none", delay,
                pre_a if pre_a is not None else "?", post_a or "none",
                inp.get("focus_kind") or "?"))


def verdicts_and_markers(inp, pre_wake_focus=None):
    """PURE replay harness entry: for ONE flow snapshot return
    (per_type_hops_dict, markers[]). markers contains every marker the live
    logic WOULD emit for exactly this input: the three per-type verbose
    [*FLOW] lines, the compact [FLOW] line, plus [STEAM-FOCUS-LOST-GAME] when
    the routing condition holds, and [STEAM-FOCUS-DROP-AFTER-WAKE] when the
    recorded pre-sleep focus was a game and the post-wake focus is not.
    pre_wake_focus = (pre_appid, pre_kind) or None (no wake boundary)."""
    per = compute(inp)
    markers = []
    for dtype, _t, _n in TYPE_ORDER:
        markers.append(type_marker(dtype, per[dtype]))
    markers.append("[FLOW] " + compact(inp, per))
    if focus_lost_condition(inp):
        markers.append(focus_lost_line(inp))
    if pre_wake_focus is not None and pre_wake_focus[1] == "GAME":
        fa = inp.get("focus_appid")
        fk = inp.get("focus_kind")
        if fk != "GAME" or (pre_wake_focus[0] not in (None, "?") and
                            fa != pre_wake_focus[0]):
            markers.append(focus_drop_line(inp, inp.get("wake_mono") or 0.0,
                                           inp.get("wake_delay") or 0.0))
    return per, markers


# ---------------------------------------------------------------------------
# LIVE wiring — build the input snapshot from S and emit on transitions/wake
# ---------------------------------------------------------------------------

def build_input(now):
    """Build the flow-snapshot dict from the live S state. Reads the resolved
    delivery state published by obs.pad_verdict_scan (which calls _game_reader_
    pid once per second) and the per-second deck/pad counters."""
    from . import discover as D   # lazy: keep module load order cycle-free
    phys = any(e.get("role") == D.ROLE_PHYSICAL
               for e in S.HID_DEVICES.values())
    game_deck = any(e.get("role") == D.ROLE_DECK
                    and e.get("deck_kind") == D.DECK_GAME
                    for e in S.HID_DEVICES.values())
    appids = sorted({a for a, _n, _c in S.STEAM_PROC_SET.values() if a})
    game_pids = ["%s:%s" % (p, n or "?")
                 for p, (a, n, _c) in sorted(S.STEAM_PROC_SET.items())]
    return {
        # H0
        "carrier": S.GYRO_SRC_CARRIER or "none",
        "phys_present": phys,
        "iio_moving": bool(S.IIO_FAST_UNTIL) and S.IIO_FAST_UNTIL > now,
        # H1
        "deck_rd": S.DECK_RD_COUNT,
        "deck_mot": S.DECK_MOTION_COUNT,
        "deck_act": S.DECK_ACTION_COUNT,
        "game_deck": game_deck,
        # H2
        "focus_appid": S.STEAM_FOCUS_APPID,
        "focus_kind": S.STEAM_FOCUS_KIND,
        "focus_label": S.STEAM_UI_FOCUS,
        "game_appids": appids,
        "game_running": bool(S.STEAM_PROC_SET),
        "game_pids": game_pids,
        # H3
        "game_reader": bool(S.PAD_GAME_READER),
        "pad_node": bool(S.PAD_NODE),
        "pad_ev": S.PAD_EV_COUNT,
        "pad_key": S.PAD_KEY_COUNT,
        "pad_stick": S.PAD_STICK_COUNT,
        "pad_gyro": S.PAD_GYRO_AXIS_COUNT,
        # wake boundary (pre-sleep focus captured at [SLEEP-ENTER])
        "pre_focus_appid": S.PRE_SLEEP_FOCUS_APPID,
        "pre_focus_kind": S.PRE_SLEEP_FOCUS_KIND,
    }


def _post_wake_due(now):
    """The monotonic of the wake for which the per-hop flow has not yet been
    emitted and WAKE_FLOW_S (~2-5 s) has elapsed, else None."""
    w = S.SLEEP_WAKE_TS
    if w is None:
        return None
    if S.FLOW_WAKE_LOGGED == w:
        return None
    if now - w < WAKE_FLOW_S:
        return None
    return w


def _game_ctx(inp):
    return inp["game_running"] or inp["game_reader"] or bool(inp["game_appids"])


def flow_scan(now):
    """(v3.13) Per-hop flow proof — called every second from obs.pad_verdict_
    scan. Emits:
      * the compact [FLOW ...] line on change (transition of any type verdict /
        routing target) and once per wake;
      * [GYRO-FLOW]/[BTNS-FLOW]/[STX-FLOW] verbose markers on per-type verdict
        transitions while a game context is live, and once per wake (~2-5 s);
      * [STEAM-FOCUS-LOST-GAME] when a running game is detectable but Steam's
        active config focus is not that game (the routing proof, never vague);
      * [STEAM-FOCUS-DROP-AFTER-WAKE] once per wake when the pre-sleep focus
        was a game and the post-wake focus is not.
    Always refreshes S.FLOW_COMPACT so every [SNAPSHOT] flow= field is current.
    Never raises."""
    try:
        inp = build_input(now)
        per = compute(inp)
        ctx = _game_ctx(inp)
        post_wake = _post_wake_due(now)
        sig = compact(inp, per)

        # -- change detection per type --------------------------------------
        type_changed = {}
        for dtype, _t, _n in TYPE_ORDER:
            k = _type_key(per[dtype])
            if k != S.FLOW_STATE.get(dtype):
                type_changed[dtype] = True
                S.FLOW_STATE[dtype] = k

        emit_compact = False
        if post_wake is not None:
            S.FLOW_WAKE_LOGGED = S.SLEEP_WAKE_TS
            emit_compact = True
            # verbose per-hop markers once per wake (~2-5 s after) — even with
            # no game the chain must be stated (H3=none / desktop).
            for dtype, _t, _n in TYPE_ORDER:
                log(type_marker(dtype, per[dtype]))
        elif type_changed:
            if ctx or S.FLOW_LAST_GAME_CTX or \
                    now - S.FLOW_LAST_LOG >= FLOW_MIN_LOG:
                emit_compact = True
                for dtype, _t, _n in TYPE_ORDER:
                    if type_changed.get(dtype) and \
                            (ctx or S.FLOW_LAST_GAME_CTX):
                        log(type_marker(dtype, per[dtype]))

        if emit_compact and sig != S.FLOW_COMPACT:
            log("[FLOW] " + sig)

        # -- routing proof markers ------------------------------------------
        if ctx and focus_lost_condition(inp):
            if not S.FOCUS_LOST_ACTIVE:
                S.FOCUS_LOST_ACTIVE = True
                S.FOCUS_LOST_LAST = now
                log(focus_lost_line(inp))
            elif now - S.FOCUS_LOST_LAST >= FOCUS_LOST_HB:
                S.FOCUS_LOST_LAST = now
                log(focus_lost_line(inp))
        elif S.FOCUS_LOST_ACTIVE:
            S.FOCUS_LOST_ACTIVE = False

        # -- wake focus-drop proof (once per wake) ---------------------------
        _focus_drop_after_wake(now)

        S.FLOW_LAST_GAME_CTX = ctx
        S.FLOW_COMPACT = sig
    except Exception as e:
        try:
            log(f"LOGGER: flow scan error: {e}")
        except Exception:
            pass


def _focus_drop_after_wake(now):
    w = S.SLEEP_WAKE_TS
    if w is None:
        return
    if S.FOCUS_DROP_LOGGED == w:
        return
    if now - w < WAKE_FLOW_S:
        return
    pre = (S.PRE_SLEEP_FOCUS_APPID, S.PRE_SLEEP_FOCUS_KIND)
    if pre[1] == "GAME":
        fa = S.STEAM_FOCUS_APPID
        fk = S.STEAM_FOCUS_KIND
        if fk != "GAME" or (pre[0] not in (None, "?") and fa != pre[0]):
            S.FOCUS_DROP_LOGGED = w
            log(focus_drop_line(build_input(now), w, now - w))


def refresh(now):
    """Refresh S.FLOW_COMPACT without emitting markers (called by obs.
    snapshot_state so every [SNAPSHOT] flow= field is current even between
    per-second flow scans). Never raises."""
    try:
        inp = build_input(now)
        S.FLOW_COMPACT = compact(inp)
    except Exception:
        pass


# Public aliases used by obs (obs imports these two names; keeping the module
# import surface minimal and cycle-free).
flow_refresh = refresh
