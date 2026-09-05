# -*- coding: utf-8 -*-
"""
ipgyro.config — the single generic runtime config (todo 52a/52b).

Reads /etc/ip-gyro-logger/config.toml with python3-stdlib `tomllib` (no third
party deps). The file carries ONLY generic tunables (cadences / sizes /
thresholds / unit names / the [labels] role->display table). It NEVER carries
VID/PID/product names — device identity is discovered at runtime (discover.py),
not configured.

Design contract (Agent.md todo 52a, A1):
  * config path default: /etc/ip-gyro-logger/config.toml (env
    IP_GYRO_LOGGER_CONFIG overrides for tests).
  * File present  -> effective values = DEFAULTS merged with the file
                     (per-key: the file wins; missing keys stay default).
  * File missing or broken -> strict DEFAULTS from conf.py; the caller logs one
                     "LOGGER: config not found/invalid, using defaults" line.
  * Never crash: load() cannot raise.

The [labels] defaults are EXACTLY the pre-52b marker strings so, with the stock
config, output stays byte-comparable on this machine (P1-P10 contract intact).

Shared mutable state is NOT kept here: app.main() calls load() once and stores
the resulting Cfg on S.CFG (and applies conf-attribute overrides for the tunable
names the runtime reads as C.<name>). This module imports nothing heavy and
imports nothing from ipgyro at module load, so it is cycle-safe.
"""

import os
import tomllib

# Default config path (installed by install.sh --log from logger/config.ipgyro.toml).
CONFIG_PATH = os.environ.get("IP_GYRO_LOGGER_CONFIG",
                             "/etc/ip-gyro-logger/config.toml")

# ---------------------------------------------------------------------------
# Strict defaults — mirrors conf.py's current constants exactly (so a missing /
# stock config behaves byte-identically to pre-52b). Only GENERIC tunables.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "log": {
        "path": "/var/log/ip-gyro-logger.log",
        "max_bytes": 300 * 1024 * 1024,
    },
    "cadence": {
        "snap": 3.0, "hb": 15.0, "iio": 1.0, "iio_idle": 5.0,
        "iio_fast": 0.1, "iio_fast_hold": 2.0, "iio_rescan": 60.0,
        "sess": 5.0, "ev_throttle": 0.1, "flow": 1.0, "hid_raw": 5.0,
        "steam_scan": 5.0, "steam_state": 30.0, "ipj_backlog": 150,
        "ipj_max_s": 25, "ipj_watchdog": 90.0, "ipj_live": 15.0,
        "gyro_src": 5.0, "dev_inv": 60.0, "pm_follow": 60.0,
    },
    "full_mode": {
        "post": 10.0, "wake": 30.0, "game": 3600.0,
    },
    "thresholds": {
        "motion_ratio": 2.0, "motion_floor": 50,
        "flow_stop": 2.0, "flow_never": 10.0, "flow_gap": 2.0,
        "flow_gap_hb": 20.0, "deck_drain": 16, "frame_min": 0.0,
    },
    "journal": {
        "inputplumber_unit": "inputplumber",
        "suspend_unit": "systemd-suspend",
    },
    "labels": {
        "role_physical": "LEGION-SRC",
        "role_deck_game": "DECK-GAME",
        "role_deck_stale": "DECK-12FB",
        "role_deck_desk": "DECK-DESK",
        "cen_imu_name_hints": ["gyro", "imu", "gyro_3d"],
    },
}


class Cfg:
    """Effective config: DEFAULTS deep-merged with the on-disk file (file wins
    per key; absent keys keep the default). Simple dot/get accessors keep the
    call sites readable without throwing on a malformed section."""

    __slots__ = ("data", "status", "path")

    def __init__(self, data, status, path):
        self.data = data
        self.status = status
        self.path = path

    def get(self, section, key, default=None):
        sec = self.data.get(section)
        if isinstance(sec, dict) and key in sec:
            return sec[key]
        if default is not None:
            return default
        # last resort: the strict default tree
        return DEFAULTS.get(section, {}).get(key)

    @property
    def labels(self):
        """Role -> display dict ([labels] section). Never None."""
        lbl = self.data.get("labels")
        return lbl if isinstance(lbl, dict) else dict(DEFAULTS["labels"])

    def num(self, section, key):
        """Numeric getter with the strict default as fallback (no crash)."""
        try:
            return float(self.get(section, key, DEFAULTS[section][key]))
        except (KeyError, TypeError, ValueError):
            return 0.0

    def int_(self, section, key):
        try:
            return int(self.num(section, key))
        except (ValueError, TypeError):
            return 0


def _deep_merge(base, over):
    """Return a merged dict (base + over; nested dicts merged per-key, else
    over wins). Both inputs must be dicts."""
    out = dict(base)
    for k, v in (over or {}).items():
        if (k in out and isinstance(out[k], dict) and isinstance(v, dict)):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    """Load the config file and return a Cfg. Never raises.

    status is one of 'loaded' | 'missing' | 'invalid' (invalid = parse error ->
    strict defaults used). Caller logs the human line with the status.
    """
    p = path or CONFIG_PATH
    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
        merged = _deep_merge(DEFAULTS, raw)
        return Cfg(merged, "loaded", p)
    except FileNotFoundError:
        return Cfg(dict(DEFAULTS), "missing", p)
    except Exception:          # tomllib.TOMLDecodeError or IOError -> strict
        return Cfg(dict(DEFAULTS), "invalid", p)
