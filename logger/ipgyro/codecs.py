# -*- coding: utf-8 -*-
"""
ipgyro.codecs — CAPABILITY codec-registry (todo 52a B3/C): decode routines are
bound to a hidraw stream by the frame PROTOCOL SIGNATURE, never by vendor or
product id. Pure data + pure functions: importing this module has no side
effects and it imports NOTHING from the package, so conf.py can re-export the
tables/headers for back-compat during migration without any import cycle.

A codec is a dict: {name, kind, probe(raw)->bool, decode(raw)->dict|None,
fmt(label, dec, raw, tag)->str, header, motion_min}. The binding key is:
  (1) the frame header signature (byte0 / byte2),
  (2) the frame length + usage,
  (3) when needed, the sysfs report-descriptor / device name.
Two 64-byte codecs ship today, moved here verbatim from conf.py decode tables
+ hidraw.py decoders (todo 52a C — "из hidraw-таблиц"):

  steamdeck64  — virtual Steam Deck controller report: byte0 = 0x01 (major_ver),
                 byte2 = 0x09 (report_type). Emitted by BOTH the UHID gaming
                 deck and the vhci desktop Steam Controller, so it is bound by
                 signature and the ROLE is carried by the caller (transport).
  xinput64     — physical XInput composite frame: byte0 = 0x04 (XInputData
                 report id); byte2 = 0x74 (XINPUT_COMMAND_ID) marks the IMU
                 payload (dec["has_imu"]). This is the physical-source frame.

The consumer (discover/capture) sniffs the FIRST frame of an opened hidraw and
binds the matching codec; a frame that matches no codec simply yields the
generic path (HIDFLOW + raw hex, no decode) — nothing breaks.

Motion thresholds: the LEGACY absolute minima (DECK_MOTION_MIN/LEGO_MOTION_MIN)
are kept here as the codec's static fallback floor; the effective per-device
threshold comes from the config [thresholds] ratio/floor (todo 52a C5) applied
by capture via codecs.motion_min_for(codec, measured_floor).
"""

import struct

# ---------------------------------------------------------------------------
# Raw-byte readers (native little-endian + the BE fields the XInput frame uses)
# ---------------------------------------------------------------------------

def _s16(raw, off):
    if off + 2 > len(raw):
        return None
    return struct.unpack_from("<h", raw, off)[0]


def _s16be(raw, off):
    """Big-endian signed i16 (the Legion XInput IMU bytes are MSB-first)."""
    if off + 2 > len(raw):
        return None
    return struct.unpack_from(">h", raw, off)[0]


def _u16(raw, off):
    if off + 2 > len(raw):
        return None
    return struct.unpack_from("<H", raw, off)[0]


def _u16be(raw, off):
    """Big-endian unsigned u16 (Legion XInput touch_x/y are MSB-first)."""
    if off + 2 > len(raw):
        return None
    return struct.unpack_from(">H", raw, off)[0]


def _u32_le(raw, off):
    if off + 4 > len(raw):
        return None
    return struct.unpack_from("<I", raw, off)[0]


# ---------------------------------------------------------------------------
# steamdeck64 — virtual Steam Deck controller report decode tables
#
# Byte-layout source of truth = InputPlumber's on-wire struct
# `PackedInputDataReport` in src/drivers/steam_deck/hid_report.rs:262-473:
#   #[packed_struct(bit_numbering = "msb0", size_bytes = "64")]
# msb0 numbering over a 64-byte struct => bit 64 is byte 8's MSB, so a
# field's byte-mask is 0x80 >> (bit % 8). Each report is serialized by
# `self.state.pack()` — steam_deck.rs:440 (USB/vhci, desktop) and
# steam_deck_uhid.rs:107 (UHID, gaming). Masks cross-checked against
# SDL_hidapi_steamdeck.c (SteamDeckButtons).
#
# Header: [0]=0x01 major_ver, [1]=0x00 minor_ver, [2]=0x09 report_type,
# [3]=0x40 report_size, [4..7]=u32 LE frame counter.
# ---------------------------------------------------------------------------

# Named buttons by byte index -> (byte-mask, name). Reserved/unnamed bits are
# kept as None and never logged.
DECK_BTN_BYTES = {
    # byte 8, bits 64-71   (hid_report.rs:280-295)
    8: ((0x80, "a"), (0x40, "x"), (0x20, "b"), (0x10, "y"),
        (0x08, "l1"), (0x04, "r1"), (0x02, "l2"), (0x01, "r2")),
    # byte 9, bits 72-79   (hid_report.rs:298-313)
    9: ((0x80, "l5"), (0x40, "menu"), (0x20, "steam"), (0x10, "view"),
        (0x08, "down"), (0x04, "left"), (0x02, "right"), (0x01, "up")),
    # byte 10, bits 80-87  (hid_report.rs:316-331)
    10: ((0x80, None), (0x40, "l3"), (0x20, None), (0x10, "r_pad_touch"),
         (0x08, "l_pad_touch"), (0x04, "r_pad_press"),
         (0x02, "l_pad_press"), (0x01, "r5")),
    # byte 11, bits 88-95  (hid_report.rs:334-349) — only r3 (bit 93 = 0x04)
    11: ((0x80, None), (0x40, None), (0x20, None), (0x10, None),
         (0x08, None), (0x04, "r3"), (0x02, None), (0x01, None)),
    # byte 13, bits 104-111  (hid_report.rs:370-385)
    13: ((0x80, "r_stick_touch"), (0x40, "l_stick_touch"),
         (0x20, None), (0x10, None), (0x08, None),
         (0x04, "r4"), (0x02, "l4"), (0x01, None)),
    # byte 14, bits 112-119  (hid_report.rs:388-403) — quick_access bit 117
    14: ((0x80, None), (0x40, None), (0x20, None), (0x10, None),
         (0x08, None), (0x04, "quick_access"), (0x02, None), (0x01, None)),
}

# stable display/compare order for a pressed-button set
DECK_BTN_ORDER = (
    "a", "b", "x", "y", "l1", "r1", "l2", "r2", "l3", "r3", "l4", "r4",
    "l5", "r5", "steam", "menu", "view", "quick_access",
    "up", "down", "left", "right",
    "l_pad_touch", "r_pad_touch", "l_pad_press", "r_pad_press",
    "l_stick_touch", "r_stick_touch",
)

# deck-protocol signature: byte0 = 0x01 major_ver, byte2 = 0x09 report_type
DECK_HEADER_BYTES = (0x01, 0x09)

# legacy static floor for "quiet" (no real rotation); superseded per-device by
# the config [thresholds] ratio/floor (todo 52a C5) when a noise floor exists.
DECK_MOTION_MIN = 50


def _deck_buttons(raw):
    """Pressed named buttons in a 64-byte deck report (deterministic order)."""
    if len(raw) < 15:
        return []
    pressed = set()
    for bi, table in DECK_BTN_BYTES.items():
        b = raw[bi]
        for mask, name in table:
            if name and (b & mask):
                pressed.add(name)
    return [n for n in DECK_BTN_ORDER if n in pressed]


def probe_steamdeck64(raw):
    """True when a raw frame carries the deck signature byte0=0x01, byte2=0x09.
    (Length check is left to decode so a short/empty probe is simply False.)"""
    if len(raw) < 64:
        return False
    return raw[0] == DECK_HEADER_BYTES[0] and raw[2] == DECK_HEADER_BYTES[1]


def decode_deck_report(raw):
    """Decode one 64-byte virtual-deck input report into a dict, or None when
    the frame does not match the deck protocol (so a 64-byte xinput64 frame is
    never mis-decoded). Axes are unpacked from the bytes cited above:
      sticks  l_stick_x/y = 48-51, r_stick_x/y = 52-55  (i16 LE)
      triggers l_trigg/r_trigg = 44-47                   (u16 LE)
      accel accel_x/y/z = 24-29                          (i16 LE)
      gyro   pitch/yaw/roll = 30-35                      (i16 LE)
    The report's own "pitch/yaw/roll" labels ARE the gyro stream (they map to
    SDL's sGyroX/Y/Z) — that is the MOTION a gyro fix must deliver."""
    if len(raw) < 64:
        return None
    if raw[0] != DECK_HEADER_BYTES[0] or raw[2] != DECK_HEADER_BYTES[1]:
        return None
    return {
        "frame": _u32_le(raw, 4),
        "buttons": _deck_buttons(raw),
        "lsx": _s16(raw, 48), "lsy": _s16(raw, 50),
        "rsx": _s16(raw, 52), "rsy": _s16(raw, 54),
        "lt": _u16(raw, 44), "rt": _u16(raw, 46),
        "ax": _s16(raw, 24), "ay": _s16(raw, 26), "az": _s16(raw, 28),
        "gx": _s16(raw, 30), "gy": _s16(raw, 32), "gz": _s16(raw, 34),
    }


def deck_fmt(label, dec, raw, tag):
    """(R2) One compact [FRAME]-style line for a deck report (used by capture
    while full capture is armed). Keeps the exact field order/content of the
    legacy per-frame line so old greps still match."""
    btns = ",".join(dec["buttons"]) or "-"
    return (f"[FRAME] {label} frame={dec['frame']} btn=[{btns}] "
            f"ls=({dec['lsx']},{dec['lsy']}) rs=({dec['rsx']},{dec['rsy']}) "
            f"lt={dec['lt']} rt={dec['rt']} "
            f"gyr=({dec['gx']},{dec['gy']},{dec['gz']}) "
            f"acc=({dec['ax']},{dec['ay']},{dec['az']}) "
            f"raw={raw.hex(' ')}")


# ---------------------------------------------------------------------------
# xinput64 — full 64-byte PHYSICAL XInput report decode tables
#
# Byte-layout source of truth = InputPlumber's on-wire struct `XInputDataReport`
# in src/drivers/lego/hid_report.rs (ReportType::XInputData = 0x04 at byte 0,
# hid_cmd 0x74 = XINPUT_COMMAND_ID at byte 2, #[packed_struct(size_bytes =
# "60")] carried in a 64-byte report). The whole 64-byte frame is decoded into
# NAMED fields (nothing dropped): header/enum/state bytes 0-13, sticks 14-17,
# every named bit of the button bytes 18-21, analog + digital triggers and
# wheel 22-25, touch 26-29, the low-quality gyro bytes + IMU timestamps 30-34,
# accel/gyro i16 (MSB-first) for BOTH IMUs 35-59, and the 60-63 trailer.
#   left_gyro_x/y/z = 41/43/45, right_gyro_y/x/z = 54/56/58  (i16 BE, gyro)
#   left_accel_x/y/z = 35/37/39, right_accel_y/x/z = 48/50/52 (i16 BE, accel)
#   right_accel is stored y@48-49/x@50-51/z@52-53 and right_gyro
#   y@54-55/x@56-57/z@58-59 (Rust field order is NOT x,y,z in the bytes).
# ---------------------------------------------------------------------------

LEGO_HEADER_BYTES = (0x04, 0x74)

# GamepadMode / ConnectedState value->name maps (mirror hid_report.rs enums).
LEGO_MODE_NAMES = {0x00: "xinput", 0x01: "dinput", 0x02: "fps"}
LEGO_STATE_NAMES = {0x02: "attached", 0x03: "detached"}

# Named buttons by byte index -> (byte-mask, name) for the physical XInput
# source. bytes 18-21 are fully mapped (Rust hid_report.rs:358-428).
LEGO_BTN_BYTES = {
    18: ((0x80, "legion"), (0x40, "quick_access"), (0x20, "thumb_l"),
         (0x10, "thumb_r"), (0x08, "up"), (0x04, "down"),
         (0x02, "left"), (0x01, "right")),
    19: ((0x80, "a"), (0x40, "b"), (0x20, "x"), (0x10, "y"),
         (0x08, "lb"), (0x04, "d_trigger_l"), (0x02, "rb"),
         (0x01, "d_trigger_r")),
    20: ((0x80, "y1"), (0x40, "y2"), (0x20, "y3"), (0x10, "m1"),
         (0x08, "m2"), (0x04, "m3"), (0x02, "view"), (0x01, "menu")),
    21: ((0x80, "mouse_click"), (0x40, "show_desktop"), (0x20, "alt_tab"),
         (0x10, "u21_3"), (0x08, "u21_4"), (0x04, "u21_5"),
         (0x02, "u21_6"), (0x01, "u21_7")),
}

# stable display/compare order for the physical XInput source button set
LEGO_BTN_ORDER = (
    "legion", "quick_access", "thumb_l", "thumb_r", "up", "down", "left",
    "right", "a", "b", "x", "y", "lb", "rb", "d_trigger_l", "d_trigger_r",
    "y1", "y2", "y3", "m1", "m2", "m3", "view", "menu",
    "mouse_click", "show_desktop", "alt_tab",
    "u21_3", "u21_4", "u21_5", "u21_6", "u21_7",
)

# legacy static floor for "quiet" XInput IMU; superseded per-device by the
# config [thresholds] ratio/floor (todo 52a C5) when a noise floor exists.
LEGO_MOTION_MIN = 60


def _lego_mode(v):
    """GamepadMode byte 9 -> Display name (mirrors hid_report.rs GamepadMode)."""
    return LEGO_MODE_NAMES.get(v, "unknown")


def _lego_state(v):
    """ConnectedState byte 12/13 -> name (mirrors hid_report.rs ConnectedState:
    0x02 attached, 0x03 detached, anything else -> connecting)."""
    return LEGO_STATE_NAMES.get(v, "connecting")


def _lego_buttons(raw):
    """Pressed named buttons in an XInput frame (deterministic order)."""
    if len(raw) < 22:
        return []
    pressed = set()
    for bi, table in LEGO_BTN_BYTES.items():
        b = raw[bi]
        for mask, name in table:
            if name and (b & mask):
                pressed.add(name)
    return [n for n in LEGO_BTN_ORDER if n in pressed]


def probe_xinput64(raw):
    """True when a raw frame is an XInputData report (byte0 = 0x04). Whether
    the frame also carries the IMU payload is decided by decode (byte2)."""
    if len(raw) < 64:
        return False
    return raw[0] == LEGO_HEADER_BYTES[0]


def decode_lego_report(raw):
    """Decode one 64-byte physical XInput report into a dict, or None when the
    frame is not an XInputData report (a 64-byte deck frame is never
    mis-decoded). Byte0 must be 0x04 (XInputData report id); when byte2 is 0x74
    (XINPUT_COMMAND_ID) the frame carries the IMU payload (dec["has_imu"]).
    EVERY field of the 64-byte frame is decoded into a named value so nothing
    in the joystick path is left as an anonymous byte:
      header/enums/state: report_id/report_size/hid_cmd, aux bytes 3/4/6/8/10/
        11/24, mode (9), l/r con_state (12/13), batteries (5/7)
      controls: sticks 14-17, button bytes 18-21 (named), analog triggers
        22/23, mouse_z 25, touch_x/y 26-29 (u16 BE)
      IMU: low-quality gyro 30-33, IMU timestamps 34/47, accel+gyro i16 BE for
        left (35-46) and right (48-59)
    All multi-byte IMU/touch fields are big-endian."""
    if len(raw) < 64:
        return None
    if raw[0] != LEGO_HEADER_BYTES[0]:
        return None
    try:
        has_imu = raw[2] == LEGO_HEADER_BYTES[1]
        return {
            "has_imu": has_imu,
            "report_id": raw[0], "report_size": raw[1], "hid_cmd": raw[2],
            "u3": raw[3], "u4": raw[4],
            "lbat": raw[5], "u6": raw[6], "rbat": raw[7], "u8": raw[8],
            "mode": _lego_mode(raw[9]), "u10": raw[10], "u11": raw[11],
            "lst": _lego_state(raw[12]), "rst": _lego_state(raw[13]),
            "lsx": raw[14], "lsy": raw[15], "rsx": raw[16], "rsy": raw[17],
            "buttons": _lego_buttons(raw),
            "alt_l": raw[22], "alt_r": raw[23], "u23": raw[24],
            "mz": raw[25],
            "tx": _u16be(raw, 26), "ty": _u16be(raw, 28),
            "lglqx": raw[30], "lglqy": raw[31],
            "rglqx": raw[32], "rglqy": raw[33],
            "lts": raw[34],
            "lax": _s16be(raw, 35), "lay": _s16be(raw, 37),
            "laz": _s16be(raw, 39),
            "lgx": _s16be(raw, 41), "lgy": _s16be(raw, 43),
            "lgz": _s16be(raw, 45),
            "rts": raw[47],
            "ray": _s16be(raw, 48), "rax": _s16be(raw, 50),
            "raz": _s16be(raw, 52),
            "rgy": _s16be(raw, 54), "rgx": _s16be(raw, 56),
            "rgz": _s16be(raw, 58),
        }
    except IndexError:
        return None


def lego_fmt(label, dec, raw, tag):
    """Format one full all-fields DECODE line for an XInput frame. Keeps the
    legacy substrings left_gyro=(x=,y=,z=) and right_gyro=(y=,x=,z=) so old
    greps for those tokens still match, and adds every other named field + aux
    bytes + trailer, with the full raw hex at the end."""
    btns = ",".join(dec["buttons"]) or "-"
    return (
        f"DECODE {label} XFULL imu={1 if dec['has_imu'] else 0} "
        f"cmd={dec['hid_cmd']:02x} mode={dec['mode']} "
        f"bat=({dec['lbat']},{dec['rbat']}) state=({dec['lst']},{dec['rst']}) "
        f"sticks=({dec['lsx']},{dec['lsy']},{dec['rsx']},{dec['rsy']}) "
        f"btn=[{btns}] "
        f"trig=({dec['alt_l']},{dec['alt_r']}) wheel={dec['mz']} "
        f"touch=({dec['tx']},{dec['ty']}) "
        f"lqgyro=({dec['lglqx']},{dec['lglqy']},{dec['rglqx']},{dec['rglqy']}) "
        f"ts=({dec['lts']},{dec['rts']}) "
        f"accel_l=({dec['lax']},{dec['lay']},{dec['laz']}) "
        f"left_gyro=(x={dec['lgx']},y={dec['lgy']},z={dec['lgz']}) "
        f"accel_r=({dec['ray']},{dec['rax']},{dec['raz']}) "
        f"right_gyro=(y={dec['rgy']},x={dec['rgx']},z={dec['rgz']}) "
        f"aux=({dec['u3']},{dec['u4']},{dec['u6']},{dec['u8']},"
        f"{dec['u10']},{dec['u11']},{dec['u23']}) "
        f"tr={raw[60:64].hex(' ')} "
        f"raw={raw.hex(' ')} [{tag}]"
    )


# Back-compat aliases (the legacy names hidraw.py used for these formatters).
_lego_fmt = lego_fmt


# ---------------------------------------------------------------------------
# The codec registry
# ---------------------------------------------------------------------------

CODECS = (
    {
        "name": "steamdeck64",
        "kind": "deck-output",          # semantic family (see discover roles)
        "label": "DECK-GAME",           # legacy default display label
        "header": DECK_HEADER_BYTES,
        "probe": probe_steamdeck64,
        "decode": decode_deck_report,
        "fmt": deck_fmt,
        "motion_min": DECK_MOTION_MIN,
        "btn_bytes": DECK_BTN_BYTES,
        "btn_order": DECK_BTN_ORDER,
    },
    {
        "name": "xinput64",
        "kind": "physical-source",      # semantic family (see discover roles)
        "label": "LEGION-SRC",          # legacy default display label
        "header": LEGO_HEADER_BYTES,
        "probe": probe_xinput64,
        "decode": decode_lego_report,
        "fmt": lego_fmt,
        "motion_min": LEGO_MOTION_MIN,
        "btn_bytes": LEGO_BTN_BYTES,
        "btn_order": LEGO_BTN_ORDER,
    },
)

# name -> codec lookup
CODEC_MAP = {c["name"]: c for c in CODECS}


def probe_codec(raw):
    """Return the first codec whose probe(raw) matches, or None."""
    for c in CODECS:
        try:
            if c["probe"](raw):
                return c
        except Exception:
            continue
    return None


def sniff(raw):
    """(codec_name, dec) for one sniffed raw frame, or (None, None) when no
    codec matched. Used by capture to bind a codec to an opened hidraw on its
    first received frame (device-id-free)."""
    c = probe_codec(raw)
    if c is None:
        return None, None
    try:
        dec = c["decode"](raw)
    except Exception:
        dec = None
    return c["name"], dec


def by_name(name):
    """Return the codec dict for a registry name, or None."""
    return CODEC_MAP.get(name)
