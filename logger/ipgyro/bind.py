# -*- coding: utf-8 -*-
"""
ipgyro.bind — v3.16 (todo 53e) ACTIVE-CONFIG GYRO-BINDING parser.

WHY THIS MODULE EXISTS (research 53d)
-------------------------------------
Steam Input routes buttons/sticks via the virtual XInput pad (28de:11ff), but
GYRO is a SEPARATE binding whose OUTPUT is decided by the ACTIVE config:
  * pad-axes (right-stick) -> gyro lands on the pad as ABS_RX/ABS_RY
  * mouse                 -> gyro is injected as a SEPARATE pointer, NOT the pad
  * disabled              -> the config disables gyro entirely
After wake Steam's focus reverts to the desktop config (413080) which binds
gyro to MOUSE -> pad ABS_RX/ABS_RY stops while buttons/sticks (still pad
outputs) keep reaching the game. The v3.15 logger wrongly treated config=413080
as a GLOBAL loss for all types and only watched the pad (missing gyro-as-mouse).

This module parses the ACTIVE Steam Input config's gyro binding from the
per-appid controller_*.vdf under
  <steam_base>/steamapps/common/Steam Controller Configs/<steamid>/config/<appid>/
and returns 'pad-axes' | 'mouse' | 'disabled' | 'unknown'. If the config file
cannot be parsed it returns 'unknown' (never guesses).

VDF STRUCTURE (observed on this host)
-------------------------------------
"controller_mappings" {
  "group" { "id" "18"  "mode" "gyro_to_joystick" ... }   # one per group
  "group" { "id" "19"  "mode" "gyro_to_mouse" ... }
  ...
  "preset" { "id" "0" "name" "Default"
    "group_source_bindings" {
      "30"  "gyro active"      # source element -> group id
      "18"  "gyro inactive"
      ...
    }
  }
}
The ACTIVE gyro binding = the mode of the group whose id is bound to the
"gyro active" source element in the Default preset:
  gyro_to_joystick -> pad-axes | gyro_to_mouse -> mouse | else disabled.

The parser is PURE (takes a file path + optional appid, returns a verdict), so
the SAME logic drives (a) the live logger and (b) the replay-sanity harness.
Shared mutable state is accessed ONLY as S.<name>.
"""

import os
import re

from . import state as S
from .logcore import log

# The deck's Steam Input controller type -> the controller_*.vdf that carries
# the deck's action map (the config Steam applies to the 28de:12f0 deck).
_DECK_CONTROLLER_TYPES = (
    "controller_steamos_handheld.vdf",
    "controller_neptune.vdf",
    "controller_steamcontroller_gordon.vdf",
)

# group mode -> binding verdict
_MODE_TO_BINDING = {
    "gyro_to_joystick": "pad-axes",
    "gyro_to_joystick_camera": "pad-axes",
    "gyro_to_mouse": "mouse",
    "gyro_to_mouse_joystick": "mouse",
    "gyro_to_2d": "mouse",
    "gyro_to_3d": "mouse",
    "gyro_to_ball": "mouse",
    "gyro_to_pointer": "mouse",
    "gyro_to_radial": "mouse",
    "gyro_to_switches": "disabled",
    "none": "disabled",
    "disabled": "disabled",
}

# ---------------------------------------------------------------------------
# Minimal VDF tokenizer / parser (only what the binding extraction needs)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'"[^"]*"|[{}]')


def _tokenize(text):
    """Split a VDF into tokens: quoted strings and { } braces."""
    return _TOKEN_RE.findall(text)


def _parse_vdf(text):
    """Parse a VDF into nested dicts. Repeated keys (e.g. the many "group" and
    "preset" blocks) are preserved as a LIST under that key (the parser appends
    to a list when a key repeats), so no group/preset is lost to a last-wins
    collapse. A key followed by '{' becomes a dict/list; a key followed by a
    value string becomes key->value. Returns the root dict."""
    toks = _tokenize(text)
    pos = [0]

    def _value():
        t = toks[pos[0]]
        pos[0] += 1
        return t.strip('"')

    def _block():
        # pos points at '{'
        pos[0] += 1  # consume '{'
        node = {}
        while pos[0] < len(toks):
            t = toks[pos[0]]
            if t == "}":
                pos[0] += 1
                return node
            if t == "{":
                # bare block (list-like) — collect into a list under a sentinel
                blk = _block()
                node.setdefault("__list__", []).append(blk)
                continue
            key = _value()
            if pos[0] >= len(toks):
                break
            nxt = toks[pos[0]]
            if nxt == "{":
                val = _block()
            else:
                val = _value()
            # preserve repeated keys as a list
            if key in node:
                if not isinstance(node[key], list):
                    node[key] = [node[key]]
                node[key].append(val)
            else:
                node[key] = val
        return node

    return _block()


def _as_list(node):
    """Normalize a parsed value (single dict OR list-of-dicts from repeated
    keys) into a list for iteration."""
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


def _find_groups(root):
    """Return {group_id: mode} from the parsed config root."""
    groups = {}

    def _walk(node):
        if isinstance(node, dict):
            if "id" in node and "mode" in node:
                gid = str(node.get("id", "")).strip()
                if gid:
                    groups[gid] = str(node.get("mode", "")).strip()
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(root)
    return groups


def _default_preset_gyro_group(root):
    """Return the group id bound to the 'gyro active' source element in the
    Default preset (id 0), or None. The gyro element is named 'gyro active' (the
    source that is live when gyro is enabled); 'gyro inactive' groups are the
    disabled fallback."""
    def _walk(node):
        if isinstance(node, dict):
            if "group_source_bindings" in node:
                gsb = node["group_source_bindings"]
                for gsb_node in _as_list(gsb):
                    if isinstance(gsb_node, dict):
                        for gid, src in gsb_node.items():
                            if str(src).strip().lower() == "gyro active":
                                return str(gid).strip()
            for v in node.values():
                r = _walk(v)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for item in node:
                r = _walk(item)
                if r is not None:
                    return r
        return None

    return _walk(root)


def _binding_from_text(text, path):
    """Parse the gyro binding from a controller vdf's text. Returns
    (binding, evidence) where binding is pad-axes|mouse|disabled|unknown."""
    try:
        root = _parse_vdf(text)
    except Exception:
        return "unknown", f"unparseable vdf {path}"
    groups = _find_groups(root)
    gid = _default_preset_gyro_group(root)
    if gid is None:
        # No 'gyro active' source binding found -> gyro is not enabled in the
        # Default preset (disabled). But be honest: if we cannot find the preset
        # structure at all, call it unknown rather than guess disabled.
        if not groups:
            return "unknown", f"no groups parsed from {path}"
        return "disabled", f"no 'gyro active' source binding in {path} (gyro off)"
    mode = groups.get(gid)
    if mode is None:
        return "unknown", f"gyro group id {gid} has no mode in {path}"
    binding = _MODE_TO_BINDING.get(mode, "unknown")
    if binding == "unknown":
        return "unknown", f"gyro group id {gid} mode={mode} unmapped in {path}"
    return binding, f"gyro group {gid} mode={mode} in {path}"


# ---------------------------------------------------------------------------
# Locating the active config file
# ---------------------------------------------------------------------------

def _steam_bases():
    """Desktop-user Steam bases (mirrors steam.py._steam_bases)."""
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


def _config_dir_for_appid(appid):
    """Return the config dir path for an appid across all Steam bases, or None.
    Path: <base>/.local/share/Steam/steamapps/common/Steam Controller Configs/
    <steamid>/config/<appid>/  (the <steamid> subdir varies per user)."""
    if not appid:
        return None
    for base in _steam_bases():
        root = os.path.join(
            base, ".local/share/Steam/steamapps/common/Steam Controller Configs")
        try:
            if not os.path.isdir(root):
                continue
            for steamid in os.listdir(root):
                cfg = os.path.join(root, steamid, "config", str(appid))
                if os.path.isdir(cfg):
                    return cfg
        except Exception:
            continue
    return None


def _candidate_vdfs(cfg_dir):
    """Ordered candidate controller vdfs in an appid config dir: the deck's own
    controller types first, then any controller_*.vdf (fallback)."""
    cands = []
    try:
        names = sorted(os.listdir(cfg_dir))
    except Exception:
        return cands
    for ct in _DECK_CONTROLLER_TYPES:
        if ct in names:
            cands.append(os.path.join(cfg_dir, ct))
    for n in names:
        if n.startswith("controller_") and n.endswith(".vdf") \
                and os.path.join(cfg_dir, n) not in cands:
            cands.append(os.path.join(cfg_dir, n))
    return cands


def read_gyro_binding(appid=None):
    """Parse the ACTIVE config's gyro binding for `appid` (default: the current
    Steam focus appid from S.STEAM_FOCUS_APPID). Returns (binding, evidence):
      binding = 'pad-axes' | 'mouse' | 'disabled' | 'unknown'
    If no config file can be found/parsed, returns ('unknown', evidence) — the
    logger NEVER guesses a binding from pad evidence alone. Reads only
    S.STEAM_FOCUS_APPID when appid is None."""
    if appid is None:
        appid = S.STEAM_FOCUS_APPID
    if not appid:
        return "unknown", "no active Steam config appid (no focus / no game)"
    cfg_dir = _config_dir_for_appid(appid)
    if cfg_dir is None:
        return "unknown", f"no Steam Controller Configs dir for appid {appid}"
    for vdf in _candidate_vdfs(cfg_dir):
        try:
            with open(vdf, "r", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        binding, ev = _binding_from_text(text, vdf)
        if binding != "unknown":
            return binding, ev
    # none of the candidates parsed to a known binding
    return "unknown", f"no parseable controller vdf with a gyro binding under {cfg_dir}"


def binding_scan(now):
    """(v3.16) Re-read the active config's gyro binding and publish it on S
    (S.GYRO_BINDING / S.GYRO_BINDING_SRC), logging [GYRO-BINDING] on change.
    Called on the steam-scan cadence. Never raises."""
    try:
        binding, ev = read_gyro_binding()
        if binding != S.GYRO_BINDING:
            S.GYRO_BINDING = binding
            S.GYRO_BINDING_SRC = ev
            log(f"[GYRO-BINDING] active config gyro output = {binding} "
                f"({ev}) — binding-aware gyro verdict will use this")
        elif now - S.GYRO_BINDING_LAST >= 60.0:
            S.GYRO_BINDING_LAST = now
            log(f"[GYRO-BINDING] (still) active config gyro output = {binding} "
                f"({ev})")
    except Exception as e:
        try:
            log(f"LOGGER: gyro binding scan error: {e}")
        except Exception:
            pass
