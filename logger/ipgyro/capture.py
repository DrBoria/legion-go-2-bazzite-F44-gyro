# -*- coding: utf-8 -*-
"""
ipgyro.capture — the per-device hidraw CAPTURE pipeline (todo 52a C), driven by
ROLE + CODEC instead of vendor/product ids. This is the role-based successor to
the old ipgyro.hidraw monitor: it opens the nodes the GENERIC role registry
(ipgyro.discover) assigns, binds each to a codec by its frame-signature, and
runs the per-report decode + the HIDFLOW / FLOW GAP / ACTIVITY liveness and the
[DETACH]/[DOCK] aggregate — with every marker/format preserved (default labels
come from config [labels] = the v3.6 strings, so output is byte-comparable).

Each S.HID_DEVICES entry now carries the semantic identity:
  {role, deck_kind, codec, label, transport, vid(obs), pid(obs), iface, name,
   node, path, fd, ...capture state}
`role` is one of discover.ROLE_*; `codec` is the bound codec name from the
frame-signature sniff (steamdeck64 / xinput64 / None for generic). vid/pid are
OBSERVABLE fields only — never selectors.

The [DETACH]/[DOCK] callbacks (_mark_detach/_mark_dock) live in ipgyro.obs, so
scan_hidraw() imports obs LAZILY inside the function body (obs imports
_hid_label from this module at module level — a lazy import keeps the two
modules import-safe with no top-level cycle).

Shared mutable state (HID_DEVICES/HID_OPEN_ERR/PHYS_STATE/EV_ACT_COUNT/
EV_ACT_LAST) is ALWAYS accessed as S.<name>; _fmt/_mag are imported by value
from ipgyro.iio.
"""

import os
import select
import time

from . import state as S
from . import codecs as CODECS
from . import discover as D
from .conf import (
    DECK_DRAIN_MAX,
    FLOW_EV_WINDOW,
    FLOW_GAP_AFTER,
    FLOW_GAP_HB,
    FLOW_NEVER_AFTER,
    FLOW_STOP_AFTER,
    FLOW_STOP_HB,
    HID_RAW_SAMPLE,
    HID_MOTION_LOG_MIN,
    LEGO_MOTION_LOG_MIN,
    LEGO_RAW_HB,
)
from .iio import _fmt, _mag
from .logcore import log


# ---------------------------------------------------------------------------
# Labels + per-device helpers
# ---------------------------------------------------------------------------

def _labels():
    """Effective [labels] dict from the loaded config (S.CFG), else defaults."""
    cfg = getattr(S, "CFG", None)
    if cfg is not None:
        try:
            return cfg.labels
        except Exception:
            pass
    return dict(D.DEFAULT_LABELS)


def _hid_label(e):
    """Display label for a device entry: its role label + '@1.<iface>' when the
    node is one interface of a composite (e.g. controller@1.2, v3.9 human)."""
    lbl = e.get("label") or e.get("role") or "?"
    if e.get("iface") is not None:
        lbl += f"@1.{e['iface']}"
    return lbl


def _full_mode_active():
    """(R2) True while full capture is armed (game session or transition
    window). Reads S directly (obs._full_mode is the same predicate) so the
    per-frame hot path never needs a lazy obs import."""
    return S.GAME_RECORD or time.monotonic() < S.FULL_RECORD_UNTIL


def _drop_hid(node):
    try:
        os.close(S.HID_DEVICES[node]["fd"])
    except Exception:
        pass
    S.HID_DEVICES.pop(node, None)


def _refresh_phys_id():
    """(v3.10 D6) Refresh S.PHYS_ID_LAST to the CURRENT healthy physical-source
    identity (sorted human labels, e.g. 'controller@1.1,controller@1.2'). Called
    while the physical source is present so a later [DETACH] captures the exact
    interface set that left; a dock that returns as a DIFFERENT set is then
    flagged [RE-ENUM] by obs.reenum_check."""
    phys = sorted(_hid_label(e) for e in S.HID_DEVICES.values()
                  if e.get("role") == D.ROLE_PHYSICAL)
    S.PHYS_ID_LAST = ",".join(phys) if phys else None


def _refresh_phys_nodes():
    """(v3.14 todo 52x) Refresh S.PHYS_NODES_LAST (the current physical-source
    node set as "label vid:pid" strings) and S.RIGHT_GYRO_PRESENT (True when a
    physical node with pid 61ed is present = the right handle is physically
    detached and its own gyro is a SEPARATE source; False when only the full
    controller 61eb with the center gyro in the right_gyro slot is attached).
    Called on every scan_hidraw pass so the [DETACH]/[DOCK] markers and the
    SNAPSHOT right_gyro= field always reflect the current physical attach state.
    The 61ed/61eb pids are OBSERVABLE identity only (never selectors) — the
    right-handle-vs-center distinction is a physical-attach fact, not a codec."""
    out = []
    right = False
    for e in S.HID_DEVICES.values():
        if e.get("role") != D.ROLE_PHYSICAL:
            continue
        lbl = e.get("label") or "?"
        if e.get("iface") is not None:
            lbl += f"@1.{e['iface']}"
        vp = ""
        if e.get("vid") and e.get("pid"):
            vp = f" {e['vid']}:{e['pid']}"
            if str(e.get("pid")).upper() == "61ED":
                right = True
        out.append(f"{lbl}{vp}")
    S.PHYS_NODES_LAST = sorted(out)
    S.RIGHT_GYRO_PRESENT = right


# ---------------------------------------------------------------------------
# Discovery + open/close
# ---------------------------------------------------------------------------

def discover_hidraws():
    """Return the GENERIC role registry {node -> role entry} for the nodes to
    capture (authoritative codec-sniff). Kept as the name the old hidraw.py
    exposed so callers/app can switch without touching the call site."""
    return D.snapshot_roles(_labels())


def scan_hidraw():
    """Open the role-registry hidraw nodes as they appear; close them when gone.
    Returns the current discovery so callers can reuse it (state tracker)."""
    from . import obs as O   # lazy: obs imports _hid_label from this module

    now = time.monotonic()
    found = discover_hidraws()
    for node, info in found.items():
        if node in S.HID_DEVICES:
            continue
        path = info["path"]
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            if now - S.HID_OPEN_ERR.get(path, 0.0) >= 30.0:
                log(f"HID: cannot open {path}: {e}")
                S.HID_OPEN_ERR[path] = now
            continue
        S.HID_OPEN_ERR.pop(path, None)
        S.HID_DEVICES[node] = {
            # semantic identity (role/codec/label from the registry)
            "role": info["role"], "deck_kind": info.get("deck_kind"),
            "codec": info.get("codec"), "label": info.get("label"),
            "transport": info.get("transport"),
            # observable identity (never selectors)
            "vid": info.get("vid"), "pid": info.get("pid"),
            "iface": info.get("iface"), "name": info.get("name") or "",
            "syspath": info.get("syspath") or "",
            "node": node, "path": path,
            # capture state
            "fd": fd, "buf": bytearray(),
            "last_data": None, "count": 0, "nbytes": 0, "last_len": 0,
            "last_hex": "", "last_dec": {}, "last_raw": 0.0,
            "stopped": False, "stopped_at": 0.0, "never_logged_stop": True,
            "first_attach": now, "frames_seen": {},
            # per-report decode / motion / frame-continuity state
            "dec_count": 0, "mot_count": 0, "mot_last": 0.0, "mot_log": 0.0,
            # v3.11 — deck user-action counter (button/stick/trigger change)
            # since the last per-second pass; feeds the deck_rd/s + [DECK→STEAM]
            # hop-1 delivery evidence (NOT proof the game got the input).
            "act_count": 0,
            "btn_prev": "", "last_frame": None,
            "gap_active": False, "gap_at": 0.0,
            "bad_len_logged": False, "bad_hdr_logged": False, "jump_log": 0.0,
            # physical XInput decode/raw state + coalesced-deck-burst flag
            "lego_prev": "", "lego_dec_log": 0.0, "lego_hex_log": 0.0,
            "lego_last_motion": False, "lego_bad_logged": False,
            "motion_seen": False,
            # flow-liveness transition state (loud DETACH markers)
            "flow_alive": False,    # reads>0 in the previous 1s summary pass
            "flow_state": None,     # last loud state: None | "ALIVE" | "DEAD"
            "flow_last_rd": 0,      # last second's rd/s while it was delivering
            # G3 consumer-delivery probe state (change-only [DECK-CONSUMER])
            "consumer_sig": None,   # (node, reader-pid-tuple) last logged
        }
        log(f"HID: capturing {path} ({_hid_label(S.HID_DEVICES[node])} "
            f"role={info['role']} codec={info.get('codec') or '-'} "
            f"transport={info.get('transport')} "
            f"vid={info.get('vid') or '?'}:{info.get('pid') or '?'})")
    for node in list(S.HID_DEVICES.keys()):
        if node not in found:
            log(f"HID: closed /dev/{node} ({_hid_label(S.HID_DEVICES[node])} gone)")
            _drop_hid(node)

    # -- aggregate [DETACH]/[DOCK]: the physical-source set -------------------
    # One loud marker when the whole physical source leaves / returns, on top
    # of the per-node HID lines above. An XInput<->DInput mode switch also shows
    # as a [DETACH]/[DOCK] pair (xinput64 node gone, another composite pid in) —
    # the [UDEV] lines carry the USB-level detail of which pid/iface changed.
    # v3.14 (todo 52x): every [DETACH]/[DOCK] carries the physical nodes that
    # left/arrived (with their observable vid:pid, so 17ef:61eb vs 61ed is
    # explicit) and refreshes the right-handle-gyro presence (a 61ed node = the
    # right handle is detached and its own gyro is a separate source).
    phys_now = any(e["role"] == D.ROLE_PHYSICAL
                   for e in S.HID_DEVICES.values())
    _refresh_phys_nodes()
    if S.PHYS_STATE is None:
        # (v3.8 F2) first baseline: detach-tracking is live from this scan on.
        S.PHYS_STATE = phys_now
        S.STARTUP_BASELINE_TS = time.monotonic()
    elif S.PHYS_STATE and not phys_now:
        S.PHYS_STATE = False
        # (v3.10 D6) remember the physical identity at detach so a dock that
        # returns as a DIFFERENT interface (controller@1.1 -> controller@1.3,
        # IMU ts reset, state=(attached,detached)) is logged as [RE-ENUM].
        S.PHYS_ID_BEFORE_DETACH = S.PHYS_ID_LAST
        # G2 owner tag + (v3.8 F1/F3) mode=desktop|game from a FORCED fresh
        # Steam poll (live focus AppID / active Steam-Input window + proc set,
        # transport only as fallback), so a desktop detach tags desktop even
        # when only the UHID GAMING deck exists. obs._detach_marker keeps the
        # line byte-compatible.
        mode_tag = O._live_mode_tag(force=True)
        log(O._detach_marker(mode_tag))
        O._mark_detach(now)
    elif not S.PHYS_STATE and phys_now:
        S.PHYS_STATE = True
        phys_n = sum(1 for e in S.HID_DEVICES.values()
                     if e["role"] == D.ROLE_PHYSICAL)
        # (v3.10 D6) refresh the healthy physical identity and, if it differs
        # from the one that detached, emit [RE-ENUM] (device identity changed
        # across the wake/detach).
        _refresh_phys_id()
        O.reenum_check(now)
        # G2 owner tag + (v3.8 F1/F3) fresh Steam context (see [DETACH] above).
        mode_tag = O._live_mode_tag(force=True)
        log(O._dock_marker(mode_tag, phys_n))
        O._mark_dock(now)
    else:
        # physical source present and unchanged: keep the healthy identity fresh
        # so a later detach captures the CURRENT interface, not a stale one.
        _refresh_phys_id()
    return found


# ---------------------------------------------------------------------------
# Per-report decode dispatch by bound codec
# ---------------------------------------------------------------------------

def _codec_of(e):
    """The bound codec dict for a device entry (by its registry codec name)."""
    name = e.get("codec")
    if not name:
        return None
    return CODECS.by_name(name)


def _is_deck(e):
    """True when the entry is a deck-output (virtual Steam controller)."""
    return e.get("role") == D.ROLE_DECK


def _is_game_deck(e):
    """True when the entry is a GAMING (uhid) deck-output."""
    return D.is_game_deck(e)


def _is_physical(e):
    """True when the entry is the physical-source."""
    return e.get("role") == D.ROLE_PHYSICAL


def _motion_min(e):
    """Effective motion threshold for a device: the config [thresholds] floor
    when set (todo 52a C5 — ratio/floor replaces the absolute per-model
    minima), else the codec's static floor."""
    cfg = getattr(S, "CFG", None)
    if cfg is not None:
        try:
            floor = cfg.num("thresholds", "motion_floor")
            if floor > 0:
                return floor
        except Exception:
            pass
    codec = _codec_of(e)
    if codec:
        return codec.get("motion_min") or 0
    return 0


def _handle_deck_reports(e, chunk, t2):
    """Per-report decode + anomaly/gap flags for a deck-output hidraw. Real deck
    reports are one 64-byte frame per hidraw read; a read may occasionally
    contain several back-to-back 64-byte frames."""
    label = _hid_label(e)
    L = len(chunk)
    if L and (L == 64 or (L > 64 and L % 64 == 0)):
        reports = [chunk[i:i + 64] for i in range(0, L, 64)]
    else:
        # expected 64 bytes; any other length on a deck node is an anomaly
        # (flagged only once a normal 64-byte frame has already been seen)
        if L and 64 in e["frames_seen"] and not e["bad_len_logged"]:
            e["bad_len_logged"] = True
            log(f"FLOW GAP {label} anomalous frame len={L} "
                f"(deck reports are 64 bytes) head={chunk[:12].hex(' ')}")
        reports = [] if L == 0 else [chunk]
    codec = _codec_of(e)
    for raw in reports:
        dec = codec["decode"](raw) if codec else None
        if dec is None:
            if len(raw) == 64 and not e["bad_hdr_logged"]:
                e["bad_hdr_logged"] = True
                log(f"FLOW GAP {label} 64-byte frame with non-deck header "
                    f"head={raw[:12].hex(' ')} (protocol mismatch?)")
            continue
        e["last_dec"] = dec
        e["dec_count"] += 1
        # -- (R2) full-mode per-frame capture: every deck report logged so every
        #    button/stick/axis "sneeze" is visible while full capture is armed.
        if _full_mode_active():
            _log_full_frame(e["node"], label, dec, raw, t2)
        # -- button-set change -> a DECODE line proving a press reached Steam
        # (hop 1: deck -> Steam). v3.11: count it as a deck user-action so the
        # [DECK→STEAM] hop-1 evidence is measurable. This is NOT proof the game
        # got the input — only pad EV events (28de:11ff) + a game reader prove
        # that ([PAD→GAME]).
        sig = ",".join(dec["buttons"])
        if sig != e["btn_prev"]:
            e["btn_prev"] = sig
            e["act_count"] += 1
            log(f"DECODE {label} frame={dec['frame']} btn=[{sig or '-'}] "
                f"ls=({dec['lsx']},{dec['lsy']}) rs=({dec['rsx']},{dec['rsy']}) "
                f"lt={dec['lt']} rt={dec['rt']} "
                f"gyr=({dec['gx']},{dec['gy']},{dec['gz']}) "
                f"raw24-35={raw[24:36].hex(' ')}")
        # -- gyro/accel motion present -> a MOTION line proving IMU reaches deck
        mag = _mag(dec["gx"], dec["gy"], dec["gz"])
        if mag > _motion_min(e):
            e["mot_count"] += 1
            e["mot_last"] = t2
            # flag the burst so handle_hidraw drains the remaining frames
            e["motion_seen"] = True
            if t2 - e["mot_log"] >= HID_MOTION_LOG_MIN:
                e["mot_log"] = t2
                log(f"MOTION {label} frame={dec['frame']} mag={mag} "
                    f"gyr=({dec['gx']},{dec['gy']},{dec['gz']}) "
                    f"acc=({dec['ax']},{dec['ay']},{dec['az']}) "
                    f"raw24-35={raw[24:36].hex(' ')}")
        # -- frame-counter continuity (bytes 4-7, u32 LE) --------------------
        # Both deck targets increment the counter once per poll() and emit one
        # report per poll, so consecutive reads normally show delta 0 or 1.
        # A backwards jump = new generation; a monotonic jump > 1 = dropped
        # reports (only flagged for the UHID gaming decks, which emit every poll).
        fr = dec["frame"]
        prev = e["last_frame"]
        e["last_frame"] = fr
        if prev is not None and fr is not None and fr != prev:
            if fr < prev:
                if t2 - e["jump_log"] >= 1.0:
                    e["jump_log"] = t2
                    log(f"GENRESET {label} frame {prev} -> {fr} "
                        f"(new generation: counter went backwards)")
            elif fr - prev > 1 and e.get("transport") == "uhid":
                if t2 - e["jump_log"] >= 1.0:
                    e["jump_log"] = t2
                    log(f"FRAMEJUMP {label} frame {prev} -> {fr} "
                        f"(skipped {fr - prev - 1} report(s) between reads)")


def _log_full_frame(node, label, dec, raw, t2):
    """(R2) One compact [FRAME] line per deck report while full capture is
    armed — every button/stick/trigger/gyro value of the live deck output is
    logged so every "sneeze" in the active controller stream is visible. The
    full raw hex is appended so nothing is lost. A 10 ms floor (<=100 lines/s
    per deck) keeps a pathological 240 frame/s burst from flooding the log
    while still capturing every distinct input change densely."""
    last = S.FRAME_LOG_LAST.get(node, 0.0)
    if t2 - last < 0.01:
        return
    S.FRAME_LOG_LAST[node] = t2
    log(CODECS.deck_fmt(label, dec, raw, "FULL"))


def _handle_lego_reports(e, chunk, t2):
    """Per-report decode + raw capture for the PHYSICAL XInput source. Real
    XInput frames are 64 bytes (report id 0x04 at byte 0); a read may hold
    several back-to-back frames. Decodes EVERY field of each 64-byte frame into
    named values (see codecs.decode_lego_report) and emits:
      * "<controller> ... raw=..." full raw hex every LEGO_RAW_HB (5 s);
      * a full "DECODE ... XFULL ..." line whenever ANY field changes (motion
        throttled to LEGO_MOTION_LOG_MIN ~10 lines/s; the first frame of a
        fresh quiet->active edge logs immediately), plus one decoded keepalive
        on the same 5 s cadence as the raw hex so at-rest frames still show
        every field as a NAMED value at least every 5 s."""
    label = _hid_label(e)
    L = len(chunk)
    if L and (L == 64 or (L > 64 and L % 64 == 0)):
        reports = [chunk[i:i + 64] for i in range(0, L, 64)]
    else:
        # expected 64 bytes; any other length on the physical source is an anomaly
        if L and 64 in e["frames_seen"] and not e["lego_bad_logged"]:
            e["lego_bad_logged"] = True
            log(f"FLOW GAP {label} anomalous frame len={L} "
                f"(physical XInput reports are 64 bytes) head={chunk[:12].hex(' ')}")
        reports = [] if L == 0 else [chunk]
    codec = _codec_of(e)
    for raw in reports:
        dec = codec["decode"](raw) if codec else None
        if dec is None:
            # 64-byte frame that is not an XInputData report -> protocol mismatch
            if len(raw) == 64 and not e["lego_bad_logged"]:
                e["lego_bad_logged"] = True
                log(f"FLOW GAP {label} 64-byte frame with non-XInput header "
                    f"raw={raw.hex(' ')} (protocol mismatch?)")
            continue
        # -- (R2) full-mode per-frame capture: every physical XInput frame logged
        #    so the SOURCE input stream (before InputPlumber) is fully visible.
        if _full_mode_active():
            _log_lego_full_frame(e["node"], label, dec, raw, t2)
        # -- full raw hex + decoded keepalive every LEGO_RAW_HB ---------------
        if t2 - e["lego_hex_log"] >= LEGO_RAW_HB:
            e["lego_hex_log"] = t2
            log(f"{label} raw={raw.hex(' ')}")
            log(CODECS.lego_fmt(label, dec, raw, "KEEP"))
        # -- ANY field changed -> full decoded all-fields line -----------------
        # (motion throttled to LEGO_MOTION_LOG_MIN; a fresh quiet->active edge
        # logs its first frame immediately by resetting the throttle stamp)
        if repr(dec) != e["lego_prev"]:
            e["lego_prev"] = repr(dec)
            if dec["has_imu"]:
                mag = _mag(dec["lgx"], dec["lgy"], dec["lgz"],
                           dec["rgx"], dec["rgy"], dec["rgz"])
            else:
                mag = 0
            if mag > _motion_min(e):
                if not e["lego_last_motion"]:
                    e["lego_last_motion"] = True
                    e["lego_dec_log"] = 0.0
                if t2 - e["lego_dec_log"] >= LEGO_MOTION_LOG_MIN:
                    e["lego_dec_log"] = t2
                    log(CODECS.lego_fmt(label, dec, raw, "IMU"))
            else:
                e["lego_last_motion"] = False
                # control/state/quiet-field change -> immediate (rare vs motion)
                if t2 - e["lego_dec_log"] >= LEGO_MOTION_LOG_MIN:
                    e["lego_dec_log"] = t2
                    log(CODECS.lego_fmt(label, dec, raw, "CHG"))
        else:
            e["lego_last_motion"] = False


def _log_lego_full_frame(node, label, dec, raw, t2):
    """(R2) One [FRAME] line per physical XInput frame while full capture is
    armed — the SOURCE input stream (before InputPlumber) is fully visible.
    A 10 ms floor (<=100 lines/s) keeps a fast burst from flooding the log."""
    last = S.FRAME_LOG_LAST.get(node, 0.0)
    if t2 - last < 0.01:
        return
    S.FRAME_LOG_LAST[node] = t2
    log(CODECS.lego_fmt(label, dec, raw, "FULL"))


def handle_hidraw(now):
    """select() on all open hidraw fds; count reports, keep a raw/decode
    sample, note first-seen frame lengths (format signature), and dispatch each
    node to its bound-codec handler by ROLE (deck-output vs physical-source) so
    a press or a gyro blip is visible the instant it reaches Steam."""
    if not S.HID_DEVICES:
        return
    fds = [e["fd"] for e in S.HID_DEVICES.values()]
    try:
        ready, _, _ = select.select(fds, [], [], 0.02)
    except InterruptedError:
        return
    for node, e in list(S.HID_DEVICES.items()):
        if e["fd"] not in ready:
            continue
        try:
            chunk = os.read(e["fd"], 4096)
        except BlockingIOError:
            continue
        except OSError as ex:
            log(f"HID: read error on /dev/{node}: {ex}, dropping")
            _drop_hid(node)
            continue
        if not chunk:
            log(f"HID: EOF on /dev/{node}, dropping")
            _drop_hid(node)
            continue
        t2 = time.monotonic()
        e["count"] += 1
        e["nbytes"] += len(chunk)
        e["last_len"] = len(chunk)
        if e["stopped"]:
            e["stopped"] = False
            log(f"HIDFLOW RESUME {_hid_label(e)} (/dev/{node} delivering data again)")
        # close an open FLOW GAP the moment the deck delivers again
        if e["gap_active"]:
            e["gap_active"] = False
            e["gap_at"] = 0.0
            log(f"FLOW GAP CLOSED {_hid_label(e)} (deck stream delivering again)")
        # first time we see a given frame length, log it (format signature)
        if (len(chunk) <= 128 and len(chunk) not in e["frames_seen"]
                and len(e["frames_seen"]) < 32):
            e["frames_seen"][len(chunk)] = t2
            log(f"HID {_hid_label(e)} FRAME len={len(chunk)} "
                f"head={chunk[:12].hex(' ')}")
        if t2 - e["last_raw"] >= HID_RAW_SAMPLE:
            e["last_raw"] = t2
            e["last_hex"] = chunk[:24].hex(" ")
        # -- dispatch by ROLE + bound codec -----------------------------------
        if _is_deck(e):
            _handle_deck_reports(e, chunk, t2)
            # coalesce a motion burst: the deck emits ~240 frame/s but we only
            # read ~20/s, so as soon as the first frame shows IMU motion, drain
            # the rest of the burst NOW (bounded, non-blocking) instead of
            # losing it until the next select tick. Read/liveness counters are
            # still incremented per read, so HIDFLOW/ACTIVITY semantics survive.
            if e["motion_seen"]:
                e["motion_seen"] = False
                for _ in range(DECK_DRAIN_MAX - 1):
                    try:
                        dchunk = os.read(e["fd"], 4096)
                    except BlockingIOError:
                        break
                    except OSError as ex:
                        log(f"HID: read error on /dev/{node} during drain: "
                            f"{ex}, dropping")
                        _drop_hid(node)
                        break
                    if not dchunk:
                        break
                    e["count"] += 1
                    e["nbytes"] += len(dchunk)
                    e["last_len"] = len(dchunk)
                    _handle_deck_reports(e, dchunk, t2)
        elif _is_physical(e):
            _handle_lego_reports(e, chunk, t2)
        e["last_data"] = t2


def _reader_pids(path):
    """PIDs of processes holding `path` (e.g. /dev/hidraw0) open — the
    CONSUMERS of that node (Steam reads the deck hidraw; InputPlumber reads the
    physical source). Scans /proc/*/fd symlinks."""
    pids = set()
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
            if target == path:
                pids.add(int(p))
                break
    return pids


def probe_consumers(now):
    """G3 (todo 52b): consumer-input-delivery probe. For each deck-output node,
    report which processes are READING it (the consumer that receives the deck
    stream — Steam). Change-only [DECK-CONSUMER] marker proves the deck output
    is actually being consumed (delivery), closing the 'no consumer probe' gap.
    Also reports the physical-source reader (InputPlumber) for correlation."""
    for node, e in list(S.HID_DEVICES.items()):
        if not (_is_deck(e) or _is_physical(e)):
            continue
        path = e["path"]
        pids = _reader_pids(path)
        key = (node, tuple(sorted(pids)))
        prev = e.get("consumer_sig")
        if prev == key:
            continue
        e["consumer_sig"] = key
        who = "deck-output" if _is_deck(e) else "physical-source"
        if pids:
            log(f"[DECK-CONSUMER] {who} /dev/{node} read by pid(s): "
                f"{','.join(str(p) for p in sorted(pids))} — stream delivered "
                f"to consumer")
        else:
            log(f"[DECK-CONSUMER] {who} /dev/{node} NO reader process — "
                f"stream NOT being consumed")


def emit_hid_summary(now):
    """Once per second, per watched hidraw:
      - v2 HIDFLOW liveness line (markers/behaviour preserved);
      - v3 FLOW GAP: a GAMING deck that went silent while the physical source
        is still active -> the exact direction/instant input stopped;
      - v3 ACTIVITY: ONE compact cross-direction correlation line
        (EV | physical-source reads | per-DECK reads + MOTION presence).
    G3: also probes the deck-output consumer (reader PIDs) change-only.
    v3.11: accumulates the per-second DECK (hop-1) counters into S
    (DECK_RD_COUNT / DECK_MOTION_COUNT / DECK_ACTION_COUNT) so obs can emit
    [DECK→STEAM] (deck frames flowing = hop 1 only, never proof the game got
    the input) and the SNAPSHOT deck_rd/s field."""
    try:
        probe_consumers(now)
    except Exception:
        pass
    # v3.11 — reset the per-second deck (hop-1) counters before accumulating.
    S.DECK_RD_COUNT = 0
    S.DECK_MOTION_COUNT = 0
    S.DECK_ACTION_COUNT = 0
    # -- pre-pass: is the physical source still delivering? -------------------
    # (computed before the per-device loop so the result is order-independent)
    src_alive = False
    src_rd = 0
    for e in S.HID_DEVICES.values():
        if not _is_physical(e):
            continue
        src_rd += e["count"]
        if e["count"] or (e["last_data"] is not None
                          and now - e["last_data"] < FLOW_EV_WINDOW):
            src_alive = True
    ev_active = bool(S.EV_ACT_LAST) and (now - S.EV_ACT_LAST) <= FLOW_EV_WINDOW
    ev_count = S.EV_ACT_COUNT
    S.EV_ACT_COUNT = 0

    deck_parts = []
    for node, e in list(S.HID_DEVICES.items()):
        label = _hid_label(e)
        reads = e["count"]
        nbytes = e["nbytes"]
        e["count"] = 0
        e["nbytes"] = 0
        # per-second decode counters (consumed here, then reset)
        dec_rd = e["dec_count"]
        mot_rd = e["mot_count"]
        e["dec_count"] = 0
        e["mot_count"] = 0
        # v3.11 — accumulate the per-second DECK (hop-1) counters into S so obs
        # can emit [DECK→STEAM] and the SNAPSHOT deck_rd/s field. Deck frames
        # flowing = hop 1 (deck -> Steam) only — NEVER proof the game got the
        # input (that needs pad EV events + a game reader, [PAD→GAME]).
        if _is_deck(e):
            act_rd = e["act_count"]
            e["act_count"] = 0
            S.DECK_RD_COUNT += reads
            S.DECK_MOTION_COUNT += mot_rd
            S.DECK_ACTION_COUNT += act_rd
        # -- loud flow-state transitions (detach-mode signal) -----------------
        # A GAMING deck (uhid) dropping from live (>0 rd/s) to 0 rd/s is the
        # exact instant Steam loses input on a detach ([DECK-DEAD]); resuming
        # is [DECK-ALIVE]. The physical source gets the same
        # [LEGION-DEAD]/[LEGION-ALIVE] when it goes quiet while still present.
        if _is_game_deck(e) or _is_physical(e):
            prev_alive = e["flow_alive"]
            this_alive = reads > 0
            e["flow_alive"] = this_alive
            if prev_alive and not this_alive:
                e["flow_state"] = "DEAD"
                mk = "[DECK-DEAD]" if _is_game_deck(e) else "[LEGION-DEAD]"
                log(f"{mk} {label} stopped delivering "
                    f"(was {e['flow_last_rd']} rd/s) — stream DROPPED here")
            elif e["flow_state"] == "DEAD" and this_alive:
                e["flow_state"] = "ALIVE"
                mk = "[DECK-ALIVE]" if _is_game_deck(e) else "[LEGION-ALIVE]"
                log(f"{mk} {label} delivering again "
                    f"({reads} rd/s) — stream resumed")
                # (R8) dock -> deck-stream-resumed latency. v3.11: this is the
                # deck stream resuming (hop 1: deck -> Steam) — the game only
                # gets input once pad EV events (28de:11ff) + a game reader are
                # seen ([PAD→GAME]); [DECK-ALIVE] is a precondition, not proof.
                if _is_game_deck(e):
                    from . import obs as O   # lazy: obs imports _hid_label here
                    O.mark_regrab(now, "deck-alive")
            if this_alive:
                e["flow_last_rd"] = reads
        if reads:
            dec = e.get("last_dec") or {}
            dstr = mstr = ""
            if _is_deck(e) and dec:
                dstr = (" gyr(p,y,r)={},{},{}".format(
                    _fmt(dec.get("gx")), _fmt(dec.get("gy")),
                    _fmt(dec.get("gz"))))
                if mot_rd:
                    mstr = " mot={}/{}".format(mot_rd, dec_rd or reads)
            log(f"HIDFLOW {label} {reads} rd/s {nbytes} B/s "
                f"len={e['last_len']}{dstr}{mstr}")
            e["stopped"] = False
            if _is_deck(e):
                deck_parts.append((label, reads, mot_rd, dec_rd))
            continue
        # -- no traffic this second -> silence / flow-stop logic --------------
        last = e["last_data"]
        if last is None:
            age = now - e["first_attach"]
            if age >= FLOW_NEVER_AFTER and e["never_logged_stop"]:
                e["never_logged_stop"] = False
                if _is_game_deck(e):
                    log(f"FLOW STOP {label} (gaming deck present but NO data "
                        f"since attach -- nothing reaching Steam, "
                        f"{age:.0f}s)")
                else:
                    log(f"FLOW IDLE {label} (event-driven iface or nothing "
                        f"reading it -- informational, {age:.0f}s since attach)")
            # v3 FLOW GAP: gaming deck attached but never delivered while the
            # user is physically active (input blocked before Steam)
            if (_is_game_deck(e) and age >= FLOW_NEVER_AFTER
                    and ev_active and not e["gap_active"]):
                e["gap_active"] = True
                e["gap_at"] = now
                log(f"FLOW GAP {label} attached {age:.0f}s, NEVER delivered "
                    f"while PHYSICAL EV ACTIVE -- input reaching Steam "
                    f"blocked here")
            elif (_is_game_deck(e) and e["gap_active"]
                    and now - e["gap_at"] >= FLOW_GAP_HB):
                e["gap_at"] = now
                log(f"FLOW GAP {label} (still never delivered "
                    f"{age:.0f}s after attach)")
            if _is_deck(e):
                deck_parts.append((label, 0, mot_rd, dec_rd))
            continue
        silent = now - last
        if _is_deck(e):
            deck_parts.append((label, 0, mot_rd, dec_rd))
        # -- v3 FLOW GAP: gaming deck quiet while source/EV still active ------
        # This is the loud "data lost HERE" marker: physical side is alive
        # (EV events and/or physical source still delivering) but the deck
        # stream that Steam reads has stopped.
        if (_is_game_deck(e) and silent >= FLOW_GAP_AFTER
                and (ev_active or src_alive)):
            if not e["gap_active"]:
                e["gap_active"] = True
                e["gap_at"] = now
                log(f"FLOW GAP {label} deck silent {silent:.1f}s while "
                    f"source continues (EV={'active' if ev_active else 'idle'}"
                    f" src={'alive' if src_alive else 'quiet'}) -- input "
                    f"reaching Steam STOPPED here")
            elif now - e["gap_at"] >= FLOW_GAP_HB:
                e["gap_at"] = now
                log(f"FLOW GAP {label} (still silent {silent:.0f}s while "
                    f"source continues)")
        # -- v2 FLOW STOP / IDLE (unchanged behaviour) ------------------------
        if silent >= FLOW_STOP_AFTER and not e["stopped"]:
            e["stopped"] = True
            e["stopped_at"] = now
            log(f"FLOW STOP {label} (no data for {silent:.1f}s, "
                f"device still present)")
        elif e["stopped"] and now - e["stopped_at"] >= FLOW_STOP_HB:
            e["stopped_at"] = now
            log(f"FLOW STOP {label} (still silent, "
                f"{now - last:.0f}s since last data)")

    # -- one compact cross-direction ACTIVITY correlation line ---------------
    if S.HID_DEVICES or ev_count or ev_active:
        act = ["EV={}{}".format(
            ev_count if ev_count < 100 else "100+",
            "!" if ev_active else "")]
        act.append("src={}rd/s{}".format(
            src_rd, "" if src_alive else "(quiet)"))
        for label, reads, mot, dec in deck_parts:
            if reads:
                act.append(f"{label}={reads}rd/s mot={mot}/{dec or reads}")
            else:
                act.append(f"{label}=0rd/s SILENT")
        log("ACTIVITY " + " | ".join(act))
