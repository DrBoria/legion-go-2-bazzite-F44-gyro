# -*- coding: utf-8 -*-
"""
ipgyro.discover — GENERIC runtime discovery (todo 52a B3/C2): the single source
of "who is active" = a role registry built by scanning SYSTEM nodes and
classifying by TRANSPORT + CAPABILITY (the frame PROTOCOL SIGNATURE), never by
hard-coded vendor/product ids.

Roles are semantic slots, not device ids:

  physical-source  the hidraw whose input stream feeds InputPlumber — a REAL
                   (non-virtual) hidraw whose frames match the xinput64 codec
                   (byte0 = 0x04 XInputData). This is the InputPlumber input.
  deck-output      a virtual Steam controller the logger mirrors the OUTPUT of.
                   Sub-kind by TRANSPORT (replaces the 12F0 vs 1205 pid branch):
                     game     = UHID virtual deck (gaming)    -> steam-deck
                     stale    = UHID virtual deck, stale form -> steam-deck-old
                     desktop  = vhci-hcd Steam Controller     -> steam-controller
  iio-imu          gyro/accel iio device (kept for the center-IMU identity).

The authoritative capability signal is the CODEC FRAME SIGNATURE (see
ipgyro.codecs): a node is a deck-output iff it is VIRTUAL and its frames match
steamdeck64; a node is a physical-source iff it is REAL and its frames match
xinput64. A virtual uhid KEYBOARD (e.g. a BT/uhid Cube keyboard) never matches
steamdeck64, so it is excluded with no vid/pid knowledge. Because roles are
confirmed by sniffing the first received frame, discovery is two-phase:

  scan_hidraw_nodes()   pure enumeration of every hidraw (transport+identity)
  classify_hidraws()    provisional role by transport + coarse pairing
  sniff_roles()         authoritative: open candidates, sniff one frame, bind
                        the codec and confirm/refine the role (called by the
                        capture open path, and by the startup role banner)

Display labels come from the caller (config [labels]; defaults here = the v3.6
strings) so the code never knows a product name. Observed identity (vid/pid/
name/syspath/node) is carried as OBSERVABLE fields only — never as selectors.
"""

import os
import re
import select
import time

from . import state as S
from . import codecs as CODECS
from .conf import HIDRAW_DIR
from .devices import is_gamepad, parse_proc_input_devices
from .logcore import log

# ---------------------------------------------------------------------------
# Role / deck-kind vocabulary (semantic, product-independent)
# ---------------------------------------------------------------------------
ROLE_PHYSICAL = "physical-source"     # real gamepad hidraw feeding InputPlumber
ROLE_DECK = "deck-output"             # virtual Steam controller (uhid or vhci)
DECK_GAME = "game"                    # uhid gaming deck  (legacy pid 12F0)
DECK_STALE = "stale"                  # uhid stale deck   (legacy pid 12FB)
DECK_DESK = "desktop"                 # vhci desktop deck (legacy pid 1205)

# label_key -> (default display label). Config [labels] overrides these.
# v3.9 (todo 52f FIX-1 CLEAR NAMING): human device labels — a user reads
# "controller" (the real built-in gamepad feeding InputPlumber), "steam-deck"
# (the virtual Steam Deck Steam binds in GAMING mode) and "steam-controller"
# (the virtual Steam Controller for the DESKTOP shell). Marker TOKENS
# ([DECK-DEAD]/[LEGION-DEAD]/...) stay byte-compatible; only these labels changed.
DEFAULT_LABELS = {
    "role_physical": "controller",
    "role_deck_game": "steam-deck",
    "role_deck_stale": "steam-deck-old",
    "role_deck_desk": "steam-controller",
}

# codec hint per role (the actual codec is BOUND by frame-sniff; this is only
# the expected family used to pick the first probe to try).
ROLE_CODEC = {
    ROLE_PHYSICAL: "xinput64",
    ROLE_DECK: "steamdeck64",
}

# Observed identity: vid:pid embedded in a sysfs device segment
# ("0003:17EF:61EB.0001" USB HID, "0005:21CE:B907.0001" bluetooth HID).
_VIDPID_RE = re.compile(r"(?:0003|0005):([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})")
# USB interface segment inside the sysfs path, e.g. "3-1:1.2/" -> iface 2
_USBIFACE_RE = re.compile(r":1\.([0-9]+)/")
# stale-deck legacy pid (observable identity, NOT a selector; refines the
# steam-deck label to the stale 'steam-deck-old' form so forensics stay clear)
_STALE_DECK_PIDS = (0x12FB,)

# how long to wait for a first frame when sniffing a candidate (seconds)
_SNIFF_WAIT = 0.4
_SNIFF_TRIES = 20


# ---------------------------------------------------------------------------
# Low-level: enumerate every /sys/class/hidraw node with transport+identity
# ---------------------------------------------------------------------------

def _readlink(path):
    """Absolute resolved path of a /sys/class/hidraw/hidrawN symlink. sysfs
    links are RELATIVE (e.g. '../../devices/virtual/.../hidraw/hidraw0'), so we
    resolve them against the class dir to get the '/sys/devices/...' form."""
    try:
        return os.path.realpath(path)
    except Exception:
        return ""


def _transport_of(target):
    """Transport marker from the sysfs DEVPATH — distinguishes the virtual
    gaming-deck (uhid), the virtual desktop Steam Controller (vhci_hcd/usbip)
    and real physical USB/BT HID — WITHOUT any pid."""
    low = target.lower()
    if "/virtual/misc/uhid/" in low or "/uhid/" in low:
        return "uhid"
    if "/vhci_hcd/" in low or "/usbip" in low:
        return "vhci"
    return "usb"


def _sysbase(target):
    """Device base shared by all nodes of one HID interface: the sysfs path up
    to (but excluding) its /hidraw/ or /input/ child segment."""
    for marker in ("/hidraw/", "/input/"):
        idx = target.find(marker)
        if idx != -1:
            return target[:idx]
    return target


def scan_hidraw_nodes():
    """Return a list of ALL hidraw nodes with transport + observed identity:
    {node, path, target, sysbase, transport, vid, pid, iface, name}.
    No vid/pid whitelist — every hidraw is enumerated and later classified."""
    out = []
    try:
        nodes = sorted(os.listdir(HIDRAW_DIR))
    except Exception as e:
        log(f"HID: cannot list {HIDRAW_DIR}: {e}")
        return out
    for node in nodes:
        if not node.startswith("hidraw"):
            continue
        target = _readlink(os.path.join(HIDRAW_DIR, node))
        if not target:
            continue
        vid = pid = ""
        m = _VIDPID_RE.search(target)
        if m:
            vid, pid = m.group(1).upper(), m.group(2).upper()
        iface = None
        mi = _USBIFACE_RE.search(target)
        if mi:
            iface = int(mi.group(1))
        out.append({
            "node": node,
            "path": "/dev/" + node,
            "target": target,
            "sysbase": _sysbase(target),
            "transport": _transport_of(target),
            "vid": vid, "pid": pid, "iface": iface,
            "name": "",
            "syspath": target.split("/hidraw/")[0] if "/hidraw/" in target else target,
        })
    return out


# ---------------------------------------------------------------------------
# Coarse pairing (provisional role hints before the authoritative codec sniff)
# ---------------------------------------------------------------------------

def _proc_by_sysbase(devices):
    """Map HID-device sysbase -> list of /proc/bus/input/devices entries that
    share it (each logical input: keyboard / mouse / gamepad of one interface)."""
    m = {}
    for d in devices:
        base = _sysbase(d.get("sysfs") or "")
        if base:
            m.setdefault(base, []).append(d)
    return m


def _paired_entries(info, proc_map):
    """Return the /proc entries sharing this hidraw's device base (may be [])."""
    return proc_map.get(info["sysbase"], [])


def _paired_name(entries):
    """Human name of the first named paired evdev, if any."""
    for d in entries:
        nm = (d.get("name") or "").strip()
        if nm:
            return nm
    return ""


def _deck_kind_of(info):
    """Sub-kind of a deck-output by transport (uhid vs vhci), with the stale
    12fb form refined by OBSERVED pid only (identity field, not a selector)."""
    if info.get("transport") == "vhci":
        return DECK_DESK
    if info.get("pid") in _STALE_DECK_PIDS:
        return DECK_STALE
    return DECK_GAME


def _label_for(role, deck_kind, labels):
    """Resolve the display label for a role from the [labels] config (defaults
    = the human v3.9 strings above, so the log stays readable with no config)."""
    if role == ROLE_PHYSICAL:
        return labels.get("role_physical") or DEFAULT_LABELS["role_physical"]
    if role == ROLE_DECK:
        if deck_kind == DECK_DESK:
            return labels.get("role_deck_desk") or DEFAULT_LABELS["role_deck_desk"]
        if deck_kind == DECK_STALE:
            return labels.get("role_deck_stale") or DEFAULT_LABELS["role_deck_stale"]
        return labels.get("role_deck_game") or DEFAULT_LABELS["role_deck_game"]
    return "?" + role


def _entry_for(info, role, deck_kind, labels):
    """Build the registry entry dict for one hidraw node."""
    return {
        "role": role,
        "deck_kind": deck_kind,
        "label": _label_for(role, deck_kind, labels),
        "codec": ROLE_CODEC.get(role),
        # observable identity (never selectors)
        "vid": info["vid"], "pid": info["pid"], "iface": info["iface"],
        "name": info.get("name") or "",
        "transport": info["transport"],
        "syspath": info["syspath"],
        "node": info["node"], "path": info["path"],
    }


# ---------------------------------------------------------------------------
# Authoritative classification: transport + codec frame-signature sniff
# ---------------------------------------------------------------------------

def _sniff_codec(path):
    """Open a hidraw read-only and wait briefly for one frame; return the bound
    (codec_name, first_raw) or (None, None). Non-blocking, bounded, safe."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None, None
    try:
        for _ in range(_SNIFF_TRIES):
            try:
                ready, _, _ = select.select([fd], [], [], _SNIFF_WAIT)
            except InterruptedError:
                continue
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, OSError):
                break
            if chunk:
                raw = chunk[:64]
                name, _dec = CODECS.sniff(raw)
                return (name, raw) if name else (None, None)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
    return None, None


def classify_hidraws(nodes, proc_map, labels=None):
    """Provisional role registry by transport + coarse pairing (no sniff yet).
    Used for the startup banner and as the pre-sniff candidate set; the
    authoritative codec binding happens in sniff_roles(). Rules:
      * virtual hidraw that pairs with a gamepad evdev OR has no evdev at all
        -> deck-output candidate (a virtual KEYBOARD pairs with a non-gamepad
           evdev and is excluded here);
      * real USB hidraw that pairs with a gamepad evdev -> physical-source
        candidate (the XInput composite's keyboard/mouse/touchpad ifaces pair
        with gamepad-named evdevs and are kept as physical-source members);
      * everything else -> excluded."""
    labels = labels or {}
    registry = {}
    for info in nodes:
        entries = _paired_entries(info, proc_map)
        paired_name = _paired_name(entries)
        info["name"] = paired_name
        gamepad = any(is_gamepad(d) for d in entries)
        if info["transport"] in ("uhid", "vhci"):
            # virtual: keep if it pairs with a gamepad OR has no evdev at all
            # (a pure virtual keyboard/mouse pairs with a non-gamepad evdev)
            if entries and not gamepad:
                continue
            deck_kind = _deck_kind_of(info)
            registry[info["node"]] = _entry_for(info, ROLE_DECK, deck_kind, labels)
        else:
            if not gamepad:
                continue
            registry[info["node"]] = _entry_for(info, ROLE_PHYSICAL, None, labels)
    return registry


def sniff_roles(labels=None):
    """Authoritative role registry: enumerate every hidraw, then open each
    candidate and bind the codec by its first frame signature. Returns
    {node -> role entry} where role is confirmed by capability:
      * virtual + steamdeck64  -> deck-output (game/desktop by transport)
      * real + xinput64        -> physical-source
      * real composite member that shares the xinput64 node's USB device but
        emits no XInput frame (keyboard/mouse/touchpad iface) -> kept as a
        physical-source member so the whole composite is one source group
      * anything else          -> excluded (generic, not captured)"""
    labels = labels or {}
    proc_devices = parse_proc_input_devices()
    proc_map = _proc_by_sysbase(proc_devices)
    nodes = scan_hidraw_nodes()
    for info in nodes:
        info["name"] = _paired_name(_paired_entries(info, proc_map))

    registry = {}
    # pass 1: authoritative codec sniff -> confirmed roles
    for info in nodes:
        codec_name, _raw = _sniff_codec(info["path"])
        if codec_name == "steamdeck64" and info["transport"] in ("uhid", "vhci"):
            deck_kind = _deck_kind_of(info)
            registry[info["node"]] = _entry_for(info, ROLE_DECK, deck_kind, labels)
        elif codec_name == "xinput64" and info["transport"] == "usb":
            registry[info["node"]] = _entry_for(info, ROLE_PHYSICAL, None, labels)
    # pass 2: real-USB siblings of a confirmed physical-source that share its
    # USB device (same vid:pid) but emitted no XInput frame are kept as members
    # of the same physical composite (keyboard/mouse/touchpad ifaces).
    phys = [e for e in registry.values() if e["role"] == ROLE_PHYSICAL]
    phys_vp = {(e["vid"], e["pid"]) for e in phys if e["vid"] and e["pid"]}
    if phys_vp:
        for info in nodes:
            if info["node"] in registry:
                continue
            if info["transport"] != "usb":
                continue
            if (info["vid"], info["pid"]) in phys_vp:
                registry[info["node"]] = _entry_for(info, ROLE_PHYSICAL, None, labels)
    return registry


def snapshot_roles(labels=None, log_banner=False):
    """Full generic role-registry scan (authoritative codec sniff). When
    log_banner, emit one line per role so the startup log proves roles were
    assigned by capability, not hard-coded ids."""
    registry = sniff_roles(labels)
    if log_banner and registry:
        log("=== GENERIC ROLE REGISTRY (capability+transport, no device ids) ===")
        for node, e in sorted(registry.items()):
            role = e["role"] + ("" if e["deck_kind"] is None
                                else f"/{e['deck_kind']}")
            log(f"[ROLE] {e['label']:10s} role={role:26s} codec={e['codec'] or '-':10s} "
                f"transport={e['transport']:5s} /dev/{node} "
                f"vid={e['vid'] or '?'}:{e['pid'] or '?'} "
                f"name={e['name'] or '?'}")
    return registry


# ---------------------------------------------------------------------------
# Role predicates (used by capture / obs / gyro once wired)
# ---------------------------------------------------------------------------

def is_physical_source(e):
    """True for a registry/device entry that is the physical gamepad source."""
    return e.get("role") == ROLE_PHYSICAL


def is_deck_output(e):
    """True for a registry/device entry that is a virtual Steam controller."""
    return e.get("role") == ROLE_DECK


def is_game_deck(e):
    """True for a GAMING (uhid) deck-output — the 'live input to Steam' slot."""
    return e.get("role") == ROLE_DECK and e.get("deck_kind") in (DECK_GAME, DECK_STALE)


def deck_mode_of(e):
    """'game' when the deck is the UHID gaming deck, 'desktop' when it is the
    vhci Steam Controller — the G2 mode tag for [DETACH]/[DOCK]/[RECONNECT-DT].

    v3.8 (todo 52e F1): this transport-based verdict is now ONLY the fallback
    used when the live Steam context is unavailable (obs._live_mode_tag first
    asks the focused-AppID / active-Steam-Input-window verdict and the running-
    game proc set, because a machine whose only deck is the UHID GAMING deck
    can otherwise never report mode=desktop for a real desktop detach)."""
    if e.get("role") != ROLE_DECK:
        return None
    return "desktop" if e.get("deck_kind") == DECK_DESK else "game"
