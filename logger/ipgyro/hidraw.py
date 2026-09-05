# -*- coding: utf-8 -*-
"""
ipgyro.hidraw — THIN FACADE (todo 52a C). The real per-device capture pipeline
now lives in ipgyro.capture (role + codec driven, no device ids) and the generic
role registry in ipgyro.discover. This module keeps the legacy public names so
existing callers (obs.py imports _hid_label; app.py imports scan_hidraw /
handle_hidraw / emit_hid_summary) keep working unchanged while the internal
dispatch is fully role/codec based.

All functions are re-exported from ipgyro.capture; nothing device-id specific
remains here.
"""

from .capture import (  # noqa: F401
    _hid_label,
    discover_hidraws,
    emit_hid_summary,
    handle_hidraw,
    scan_hidraw,
)
