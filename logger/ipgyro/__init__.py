# -*- coding: utf-8 -*-
"""ipgyro — pure-logger-internal package for ip-gyro-logger (the passive
Legion Go 2 gyro diagnostic daemon). The monolithic logger/ip-gyro-logger.py
was split into this package on 2026-09-05 (todo 51c) with EXACTLY preserved
behaviour (every log-line format, marker and cadence); the thin launcher
logger/ip-gyro-logger.py keeps the original entry path so the systemd unit and
install.sh are untouched.

Submodules: conf (constants/regexes/tables), state (shared mutable globals),
logcore (log/rotation), devices (proc/evdev/session), iio (IMU sampling),
udev (udevadm monitor), hidraw (deck+source monitor/decode), obs (state/
snapshot/full-mode/PM/game orchestration), ipj (InputPlumber journal mirror),
steam (Steam registry/controller_ui/procs), pm (reliable systemd-suspend
journal follower), gyro (center-IMU identity + gyro-source arbiter verdict),
app (main loop).

v3.6 (todo 51d "ЛОГ ВСЁ" audit) added pm.py + gyro.py and the new markers
[CEN-IMU]/[GYRO-SRC]/[DEV-INV]/[FRAME]/[IPJ-ADD|REMOVE|CREATE|DESTROY|SUSPEND]/
[GAME-BIND]/[GAME-LOSS]/[REGRAB-DT] plus the reliable [SLEEP-ENTER]/[SLEEP-WAKE]
follower. This __init__ imports nothing heavy on purpose.

v3.7 (todo 52b) — single-source generic refactor: config.py (tomllib
/etc/ip-gyro-logger/config.toml, [labels] never VID/PID), codecs.py (decode/
probe/fmt registry keyed by frame-header signature), discover.py (role registry
physical-source / deck-output by transport+codec sniff, no device ids),
capture.py (per-device role+codec pipeline; hidraw.py is now a thin facade),
udev/obs/gyro/steam/ipj/app re-wired role-based + G1 (Steam-side
grab/release/regrab), G2 (mode/owner tags), G3 (consumer-delivery probe),
G4 (real-PID IPJ watchdog).

v3.8 (todo 52e) — desktop-detach VISIBILITY fix (a real no-game desktop detach
was invisible/mislabeled): F1 mode=game|desktop now comes from the LIVE Steam
context (focused AppID / active Steam-Input window + running-game proc set) at
the moment of a [DETACH]/[DOCK]/[RECONNECT-DT], with the transport-based
deck_mode_of only as the fallback when Steam context is unavailable; F2 the
startup blind window is closed (udev-recorded physical churn is replayed and
[STARTUP-WINDOW] synthesized before the cadence loop); F3 a fresh Steam poll is
forced at every physical change; F4 game=yes means the ACTIVE Steam-Input
window is a game (focused AppID), so lingering ESO/Proton/launcher procs no
longer keep game=yes after the user is back on the desktop shell.
"""

__version__ = "v3.8"
