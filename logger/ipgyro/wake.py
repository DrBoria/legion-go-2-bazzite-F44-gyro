# -*- coding: utf-8 -*-
"""
ipgyro.wake — D2/D3/D5/D6: the POST-WAKE DENSE CAPTURE thread (todo 52l, v3.10).

Why a dedicated thread
----------------------
The documented ~24-33 s main-loop stalls (forensically: WHOLE-PROCESS freezes —
all daemon threads silent too — from log() blocking on its stdout->journald
mirror under heavy full-capture output; the D1 root fix lives in logcore.py)
mean the cadence loop can NOT be trusted to sample the first seconds after
[SLEEP-WAKE] — exactly the window (InputPlumber's gyro arbiter re-init at
+2-3 s) that a stall used to leave UNSAMPLED. This module runs that window in a
daemon thread spawned by the pm journal follower the INSTANT a real [SLEEP-WAKE]
is journal-confirmed, and by obs.pm_wake_check's mono-gap backstop as a
fallback (both guarded so only one thread runs per wake):
  D2 — force-reads the deck hidraw frame counter + physical XInput right-gyro +
       center IIO at WAKE_BURST_IV (0.1 s) for WAKE_BURST_S (30-60 s);
  D3 — emits [GYRO-FEED] verdicts at ~2 s and ~10 s ("did the gyro feed resume" —
       a loud value-level answer instead of inference from sparse frames);
  D6 — re-asserts [CEN-IMU]/[GYRO-SRC] at 1 s for the first WAKE_REASSERT_S;
  D5 — flushes the ipj tail once at wake (delayed arbiter/gyro-center
       flip-flops arrive wake-ordered with the real journal ts + delay tag).

The thread opens its OWN read-only O_NONBLOCK fds on the deck/physical hidraw
nodes: a second reader on /dev/hidrawN receives the same duplicated stream, so
it never interferes with the cadence loop's decode, and is immune to the main
loop closing/reopening its own fds. It never mutates S.HID_DEVICES. Center IIO
is sampled from /sys/bus/iio through the iio helpers after one fresh
discover_iio() (the IMU may re-enumerate across wake).

Module-load order is cycle-free: it imports state/conf/logcore at module level
and pulls capture/iio/gyro/ipj/discover/codecs LAZILY inside the bodies that
need them (pm.py/obs.py import wake only lazily at runtime).
"""

import os
import threading
import time

from . import conf as C
from . import state as S
from .logcore import log

# spawn guard so the follower AND the backstop can both call spawn_wake_burst()
# without ever running two dense windows over one wake.
_WAKE_SPAWN_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Assessment helpers (pure-ish; exposed for the standalone smoke to exercise
# the [GYRO-FEED] verdict text without real hardware).
# ---------------------------------------------------------------------------

def assess_feed(nodes, cen_present):
    """Aggregate the per-node stats collected in one dense window into the
    [GYRO-FEED] verdict fields.
    `nodes`: list of node stat dicts from the sampler (keys: deck (bool),
    frames_s, alive, gyro ('live'|'zero'|'none')).
    Returns (deck_alive, deck_fps, phys_present, phys_gyro)."""
    deck_alive = "silent"
    deck_fps = 0.0
    phys_present = "absent"
    phys_gyro = "none"
    for nd in nodes:
        if nd.get("deck"):
            if nd.get("alive"):
                deck_alive = "alive"
            deck_fps += nd.get("frames_s", 0.0)
            if nd.get("gyro") == "live":
                deck_alive = "alive"  # frames carrying motion prove the deck is live
        else:
            if nd.get("alive"):
                phys_present = "present"
            g = nd.get("gyro")
            if g == "live":
                phys_gyro = "live"
            elif g == "zero":
                phys_gyro = phys_gyro if phys_gyro == "live" else "zero"
            elif phys_present == "present" and g == "none":
                # source delivering frames whose IMU payload never went live:
                # the arbiter did not resume gyro -> report as zeroed.
                phys_gyro = phys_gyro if phys_gyro == "live" else "zero"
    return deck_alive, deck_fps, phys_present, phys_gyro


def gyro_feed_line(t, deck_alive, deck_fps, phys_present, phys_gyro, cen_present):
    """The [GYRO-FEED] verdict line — the loud "did it resume" answer."""
    return (f"[GYRO-FEED] t=+{t:.1f}s deck={deck_alive} "
            f"deck_frames/s={deck_fps:.0f} physical={phys_present} "
            f"physical_gyro={phys_gyro} center_iio={cen_present}")


# ---------------------------------------------------------------------------
# Own-fd opening / closing (a private duplicated stream, no S.HID_DEVICES race)
# ---------------------------------------------------------------------------

def _open_own_fds():
    """Open our OWN read-only O_NONBLOCK fd on each deck/physical hidraw node
    currently in S.HID_DEVICES (by path). A second reader on the node receives
    the same stream — it can never steal frames from the cadence loop and is
    immune to the loop closing/reopening its fds. Returns a list of node dicts:
    {role,node,label,path,codec,fd,alive,prev_alive,frames_s,gyro}."""
    from . import discover as D  # lazy: keep module-load order cycle-free
    nodes = []
    for node, e in list(S.HID_DEVICES.items()):
        role = e.get("role")
        if role not in (D.ROLE_DECK, D.ROLE_PHYSICAL):
            continue
        path = e.get("path")
        if not path:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            # cannot open (detached / vanished): still carry the node so the
            # verdict reports physical=absent / deck=silent with the label.
            fd = None
        nodes.append({
            "role": role,
            "deck": role == D.ROLE_DECK,
            "node": node, "label": e.get("label") or role,
            "path": path, "codec": e.get("codec"),
            "fd": fd,
            "sec_frames": 0, "frames_s": 0.0,
            "alive": False, "prev_alive": False,
            "gyro": "none",      # 'live' | 'zero' | 'none'
            "motion_frames": 0,
        })
    return nodes


def _close_own_fds(nodes):
    for nd in nodes:
        if nd.get("fd") is not None:
            try:
                os.close(nd["fd"])
            except Exception:
                pass
            nd["fd"] = None


def _drain_node(nd):
    """Drain one own fd once (bounded, non-blocking); count 64-byte frames and
    flag whether a decoded frame carried live gyro. Never raises."""
    if nd.get("fd") is None:
        return
    try:
        chunk = os.read(nd["fd"], 65536)
    except BlockingIOError:
        return
    except OSError:
        # fd vanished underneath us -> drop it, report via alive=False
        try:
            os.close(nd["fd"])
        except Exception:
            pass
        nd["fd"] = None
        return
    if not chunk:
        return
    nframes = len(chunk) // 64
    if nframes:
        nd["sec_frames"] += nframes
        nd["alive"] = True
    # decode a sample of the drained frames to decide gyro live/zero
    if nframes:
        dec = _decode_sample(nd, chunk)
        if dec is not None:
            if dec == "live":
                nd["gyro"] = "live"
                nd["motion_frames"] += 1
            else:
                if nd["gyro"] != "live":
                    nd["gyro"] = "zero"


def _decode_sample(nd, chunk):
    """Decode one representative 64-byte frame of a drained chunk and return
    'live' (gyro magnitude above the motion floor) | 'zero' (frame carried an
    IMU payload but no real motion) | None (undecodable sample)."""
    from . import codecs as CODECS  # lazy
    c = CODECS.by_name(nd.get("codec") or "")
    if c is None:
        c = CODECS.by_name("steamdeck64" if nd.get("deck") else "xinput64")
    if c is None:
        return None
    raw = chunk[:64]
    try:
        dec = c["decode"](raw)
    except Exception:
        return None
    if dec is None:
        return None
    if nd.get("deck"):
        mag = (abs(dec.get("gx") or 0) + abs(dec.get("gy") or 0)
               + abs(dec.get("gz") or 0))
    else:
        # physical source: the InputPlumber arbiter reads RIGHT gyro
        mag = (abs(dec.get("rgx") or 0) + abs(dec.get("rgy") or 0)
               + abs(dec.get("rgz") or 0))
    if mag > (c.get("motion_min") or 0):
        return "live"
    if dec.get("has_imu") is not False:
        return "zero"
    return None


def _center_raw():
    """Read the attached center body IMU raw gyro now (entry from S.CEN_IMU),
    or None when no center IMU is discovered/sampled."""
    cen = S.CEN_IMU
    if not cen:
        return None
    entry = cen.get("entry")
    from . import iio as IIO  # lazy
    for e, dpath, _name, gyro, _acc, _scale in S.IIO_DEVICES:
        if e == entry:
            try:
                gx, gy, gz = IIO._read_axes(
                    [os.path.join(dpath, f) for f in gyro])
                return (gx, gy, gz)
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# The wake-burst thread body
# ---------------------------------------------------------------------------

def _run(wake_at):
    """Daemon thread body: dense 0.1 s sampling of deck + physical + center IIO
    for WAKE_BURST_S after a real [SLEEP-WAKE]. Logs [WAKE-BURST] dense lines,
    [GYRO-FEED] verdicts at ~2 s/~10 s, post-wake [CEN-IMU]/[GYRO-SRC] re-asserts
    at 1 s, and flushes the ipj tail once (D5)."""
    S.WAKE_BURST_ACTIVE = True
    S.WAKE_BURST_START = wake_at
    window = max(0.1, getattr(C, "WAKE_BURST_S", 60.0))
    iv = max(0.01, getattr(C, "WAKE_BURST_IV", 0.1))
    end = wake_at + window
    S.WAKE_BURST_UNTIL = end
    feed_s = tuple(getattr(C, "GYRO_FEED_S", (2.0, 10.0)))
    reassert_s = getattr(C, "WAKE_REASSERT_S", 30.0)
    log(f"[WAKE-BURST] thread start — dense {window:.0f}s deck/physical/IIO "
        f"sampling @{iv * 1000:.0f}ms armed after [SLEEP-WAKE] "
        f"(independent of the cadence loop)")

    # D5 — flush the ipj tail once: pull delayed arbiter/gyro-center lines the
    # -f follower delivered late across resume (wake-ordered with the delay).
    try:
        from . import ipj as IPJ  # lazy
        IPJ.ipj_wake_flush(wake_at)
    except Exception:
        pass

    # one fresh iio rediscover + center re-assert: the IMU can re-enumerate
    # across wake (the arbiter re-initialized at +2-3 s on this host).
    try:
        from . import gyro as G  # lazy
        from . import iio as IIO  # lazy
        IIO.discover_iio()
        G.center_imu_scan(wake_at)
    except Exception:
        pass

    nodes = _open_own_fds()
    n_open = sum(1 for nd in nodes if nd.get("fd") is not None)
    log(f"[WAKE-BURST] opened {n_open}/{len(nodes)} own deck/physical fd(s) "
        f"({','.join(nd['label'] for nd in nodes) or 'none'})")
    try:
        next_summary = wake_at
        feed_done = set()
        last_t = -1.0
        last_dense = 0.0
        while not S.STOP:
            now = time.monotonic()
            if now >= end:
                break
            t = now - wake_at
            # -- dense 0.1 s sampling --------------------------------------
            for nd in nodes:
                _drain_node(nd)
            # -- 1 s cadence: summary + verdicts + re-asserts ---------------
            if now - next_summary >= 0.9 or t - last_dense >= 1.0:
                next_summary = now
                last_dense = t
                # per-second rates (reset the rolling second counters)
                for nd in nodes:
                    nd["frames_s"] = nd["sec_frames"]
                    nd["prev_alive"] = nd["alive"]
                    nd["sec_frames"] = 0
                cen = S.CEN_IMU
                cen_present = "present" if cen else "absent"
                raw = _center_raw()
                g = ("gyro=({},{},{})".format(*raw) if raw else "gyro=NA")
                # -- [GYRO-FEED] verdicts at the configured instants ---------
                for inst in feed_s:
                    if t >= inst and inst not in feed_done:
                        feed_done.add(inst)
                        dk, fps, pp, pg = assess_feed(nodes, cen_present)
                        log(gyro_feed_line(t, dk, fps, pp, pg, cen_present))
                # -- dense [WAKE-BURST] line each second ----------------------
                parts = []
                for nd in nodes:
                    if nd.get("deck"):
                        live = "alive" if nd.get("alive") else "silent"
                        parts.append(
                            f"deck={nd['label']}:{live} "
                            f"{nd['frames_s']:.0f}fr/s "
                            f"gyro={nd['gyro']}")
                    else:
                        pres = "present" if nd.get("alive") else "absent"
                        parts.append(
                            f"physical={nd['label']}:{pres} "
                            f"{nd['frames_s']:.0f}fr/s "
                            f"gyro={nd['gyro']}")
                if not parts:
                    parts.append("deck=silent physical=absent")
                log(f"[WAKE-BURST] t=+{t:05.1f}s " + " ".join(parts)
                    + f" cen_iio={cen_present} {g}")
                # -- D6: post-wake [CEN-IMU]/[GYRO-SRC] re-asserts @1s --------
                if t <= reassert_s:
                    _post_wake_reasserts(t)
                # -- also sample iio so the value stream is in the log ---------
                try:
                    from . import iio as IIO  # lazy
                    IIO.sample_iio(now)
                except Exception:
                    pass
                for nd in nodes:
                    nd["alive"] = False
            # -- sleep to the next dense tick --------------------------------
            nap = iv - (time.monotonic() - now)
            if nap > 0:
                time.sleep(nap)
    finally:
        _close_own_fds(nodes)
        S.WAKE_BURST_ACTIVE = False
        log(f"[WAKE-BURST] thread end after {time.monotonic() - wake_at:.1f}s "
            f"(dense post-wake capture complete)")


def _post_wake_reasserts(t):
    """D6 — [CEN-IMU] + [GYRO-SRC] re-asserted every 1 s in the wake window so
    the arbiter flip-flops (13:02:08 keep / 13:02:11 filter on this host) are
    correlated to the wake instant instead of arriving 11-14 s late on the
    collapsed 60 s/5 s cadence."""
    from . import gyro as G  # lazy
    try:
        cen = S.CEN_IMU
        if cen is not None:
            log(f"[CEN-IMU] post-wake t=+{t:05.1f}s center={cen['entry']} "
                f"name={cen['name']}")
        else:
            log(f"[CEN-IMU] post-wake t=+{t:05.1f}s center=none "
                f"(no gyro/imu iio device discovered yet)")
    except Exception:
        pass
    try:
        verdict = G._gyro_src_verdict()
        log(f"[GYRO-SRC] post-wake t=+{t:05.1f}s active={verdict}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public spawn (guarded — follower + backstop both call it, one window runs)
# ---------------------------------------------------------------------------

def spawn_wake_burst(wake_mono):
    """Start the post-wake dense capture thread once per wake (idempotent while
    a window is already running). `wake_mono` = the monotonic wake instant.
    Called by pm._pm_emit on the journal-confirmed [SLEEP-WAKE] and by
    obs.pm_wake_check's mono-gap backstop."""
    now = time.monotonic()
    with _WAKE_SPAWN_LOCK:
        if S.WAKE_BURST_ACTIVE and now < S.WAKE_BURST_UNTIL:
            return
        if now - wake_mono > getattr(C, "WAKE_BURST_S", 60.0) + 5.0:
            # wake too old to matter (e.g. a very late backstop) — still run a
            # short window so the wake is never left completely unsampled.
            wake_mono = now
    try:
        threading.Thread(target=_run, args=(wake_mono,), daemon=True).start()
    except Exception as e:
        log(f"LOGGER: wake burst thread start error: {e}")
        S.WAKE_BURST_ACTIVE = False
