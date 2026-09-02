# legion-go-2-bazzite-F44-gyro

Gyroscope fix for the **Lenovo Legion Go 2** on **Bazzite (Fedora 44)**, based on a modified [InputPlumber](https://github.com/ShadowBlip/InputPlumber) build from source.

## What it fixes

- **Controllers attached** → the **central** gyroscope is active.
- **Controllers detached** → only the **right-handle** gyroscope is active.

Upstream exposes a single gyro source and mixes the center/right sensors incorrectly. This build makes the central gyro work when the controllers are attached and keeps only the right-handle gyro when they are detached.

<div align="center">

<big><strong>⚠️ STEAM INPUT REFERENCE — DON'T FORGET TO TURN THIS SETTING OFF.</strong></big>

See the screenshot below:

![Steam Input gyro — General / choose gyro button(s) — set to None (Gyro Always On)](steam-input-ref.png)

</div>

1. Open the Steam overlay → **Gyro Behavior**.
2. On the right side there is a dropdown and a gear icon; click the **gear icon** (it is located above the gyro sensitivity slider).
3. At the very top where it says **GENERAL** there will be **"choose gyro button(s)"**; click it and set it to **None (Gyro Always On)**.
4. **Reason:** by default the gyro is only enabled while a specific button is held (like Push-to-Talk), so without this change the gyro appears dead.

## What's new in v8

**v8** is the current release. Prebuilt binary sha256:
`0618564a6194f89ca8039f4db56996ac43e05c05a23f2f80d03bbea2022689ca`.

### 1. Gaming-mode fix — Steam registers the full controller (`28de-12f0`, not `28de-12fb`)

**Problem.** Bazzite / OGU (Open Game UI) force the `deck-uhid` target in gaming mode. That target previously presented vendor ID `0x12fb` ("Lenovo Legion Go 2"), which Steam classified as a **"SteamOS Handheld Controller"** (`28de-12fb`) → only **A + B** worked — no joysticks, no gyro.

**Fix** ([`steam_deck_uhid.rs`](src/input/target/steam_deck_uhid.rs)): the "Lenovo Legion Go 2" branch now presents vendor **"Valve Corporation"**, name **"Steam Controller"**, `ProductId::Generic = 0x12f0`. Steam now registers **`28de-12f0`** → full buttons + gyro in gaming mode. Confirmed on-device from the logs: Steam registered `28de-12f0` (not `12fb`) during the gaming-mode `deck-uhid` windows.

### 2. Input latency optimization (POLL_RATE 2500µs → 1000µs)

`src/input/source/mod.rs` poll rate was reduced from 2500µs to 1000µs; the InputPlumber-added latency dropped to ~1–1.5 ms.

### 3. Passive diagnostic logger (two-mode install)

`install.sh` now supports **two modes**: a plain `./install.sh` (binary install only) and `./install.sh --log` (`-l` / `--logger` are aliases), which additionally installs and enables the passive [diagnostic logger](#diagnostic-logger-v8-optional). See the install section below.

## Install (prebuilt binary)

Download **`inputplumber-legiongo2-gyro-v8.tar.gz`** from the **Releases** page, extract it and run:

```bash
tar xzf inputplumber-legiongo2-gyro-v8.tar.gz
./install.sh        # plain install — binary + profile + power fixes + auto gyro-reset unit, restarts inputplumber, ensures the logger is OFF
./install.sh --log  # same as above PLUS installs & enables the passive diagnostic logger (ip-gyro-logger.service)
```

`install.sh` installs **five** things, plus one **optional** sixth:

1. the modified binary to `/opt/inputplumber-legiongo2-runtime/`;
2. the composite device profile [`50-legion_go_2.yaml`](50-legion_go_2.yaml) to `/etc/inputplumber/devices.d/` — this routes the Legion Go 2 to the **`deck` (Steam Deck)** target, so Steam sees a gyro-capable controller instead of an Xbox Elite (which has no gyroscope);
3. the systemd override with comfortable gains (see below);
4. the suspend/resume power fix — enables `inputplumber-suspend.service` (so the device can sleep without waking from the controllers) and installs a drop-in that force-re-scans udev on wake so the virtual Steam Deck controller returns to Steam (see [Suspend / Resume fixes](#suspend--resume-fixes-included-in-this-patch));
5. the boot-time auto-reset unit [`steam-deck-uhid-gyro-reset.service`](steam-deck-uhid-gyro-reset.service) — clears Steam's virtual gamepad registry before Steam starts so the virtual Steam Deck controller re-registers **with its IMU/gyro initialized** after Bazzite updates (see [Auto-reset after updates](#4-auto-reset-after-updates-installed-by-installsh));
6. **optionally** the passive diagnostic logger (only with `--log`; plain `./install.sh` makes sure it is fully removed — see [Diagnostic logger](#diagnostic-logger-v8-optional)).

Or manually:

```bash
sudo mkdir -p /opt/inputplumber-legiongo2-runtime
sudo cp inputplumber-legiongo2-gyro-v4.resume-gamefix /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v4.resume-gamefix
sudo chmod +x /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v4.resume-gamefix

sudo mkdir -p /etc/inputplumber/devices.d
sudo cp 50-legion_go_2.yaml /etc/inputplumber/devices.d/50-legion_go_2.yaml   # Steam Deck target routing

sudo systemctl restart inputplumber
```

### Settings (recommended)

Gains are set per source via environment variables. **`install.sh` writes these automatically** to `/etc/systemd/system/inputplumber.service.d/override.conf` (with the correct `ExecStart`). The code default is `50.0` when unset — far too strong — so this override is required for comfortable play.

| Variable | Controls | Recommended (set by install.sh) |
|---|---|---|
| `IP_GYRO_GAIN_CENTER` | central gyro gain | `3.0` |
| `IP_GYRO_GAIN_HANDLE` | right-handle gyro gain | `5` |

To tweak the values:

```bash
sudo systemctl edit inputplumber.service
# [Service]
# Environment=IP_GYRO_GAIN_CENTER=3.0
# Environment=IP_GYRO_GAIN_HANDLE=5
sudo systemctl restart inputplumber
```

## Diagnostic logger (v8, optional)

In gaming mode the virtual Steam Deck controller could previously map only A/B (no joysticks, no gyro). To make such issues diagnosable after the fact, this repo ships a **passive diagnostic logger**: pure Python 3 standard library, no dependencies, no rebuild.

Files in [`logger/`](logger/):

- `logger/ip-gyro-logger.py` — records timestamped, greppable evidence: udev device add/remove events, `/proc/bus/input/devices` snapshots, IIO gyro/accel samples, evdev key/axis events, and a heartbeat / session (gamescope/desktop) transitions.
- `logger/ip-gyro-logger.service` — systemd unit, runs as **root** (so `/dev/input/*` and `/sys/bus/iio` are readable), `Restart=always`, logs to `/var/log/ip-gyro-logger.log`.

### Two install modes

```bash
./install.sh        # installs the patch AND removes the logger (idempotent): stop+disable service, delete unit,
                    # /opt/ip-gyro-logger and /var/log/ip-gyro-logger.log
./install.sh --log  # installs the patch AND installs + enables the logger (idempotent)
# -l and --logger are aliases for --log
```

Both modes are verified on-device. Re-running either mode leaves a clean state.

### How to read the logs

The logger writes to both the log file and stdout (systemd journal):

```bash
sudo tail -f /var/log/ip-gyro-logger.log     # live log
journalctl -u ip-gyro-logger -e              # same output, via the journal
```

What each kind of line means:

- `UDEV DEVADD/DEVREM ...` — devices being (re)created/removed on mode switches;
- `=== SNAPSHOT ===` — the virtual Steam Deck device is present;
- `IIO <dev> gyro=... accel=...` — the IMU is producing data;
- `EV <dev> KEY/ABS ...` — physical input is reaching the kernel;
- `SESSION ...` — gamescope/desktop session transitions.

For the Steam side, check Steam's own log after a gaming-mode window:

```bash
tail -n 200 ~/.local/share/Steam/logs/controller.txt
```

Steam registering **`28de-12f0`** (not `28de-12fb`) during the gaming-mode `deck-uhid` window is the expected v8 behaviour.

## Build from source

```bash
git clone "$(cat SOURCE_UPSTREAM)" InputPlumber
cd InputPlumber
git checkout "$(cat ../SOURCE_BASE_COMMIT)"
git apply ../patches/inputplumber-legion-go-2-bazzite.patch
cargo build --release
# result: target/release/inputplumber
```

To fine-tune or reproduce the debugging, see [Agent.md](Agent.md) — it documents every hypothesis tested, the measurements, and how to reproduce each step.

## Repository contents

- `patches/inputplumber-legion-go-2-bazzite.patch` — the complete source patch (all changes vs upstream base)
- `inputplumber-legiongo2-gyro-v4.resume-gamefix` — prebuilt modified binary for v8 (Release asset: `inputplumber-legiongo2-gyro-v8.tar.gz`); sha256 `0618564a6194f89ca8039f4db56996ac43e05c05a23f2f80d03bbea2022689ca`
- `inputplumber-legiongo2-gyro` — legacy filename for the same v8 binary (identical sha256), kept as the `install.sh` fallback
- `50-legion_go_2.yaml` — composite device profile (routes the device to the `deck` target so it is seen as a Steam Deck with gyro)
- `install.sh` — install / update script with two modes (plain vs `--log`) for the optional diagnostic logger (binary + profile + gain override + suspend/resume power fixes + boot-time Steam gyro auto-reset unit)
- `logger/` — passive diagnostic logger (`ip-gyro-logger.py` + `ip-gyro-logger.service`), installed by `./install.sh --log` (see [Diagnostic logger](#diagnostic-logger-v8-optional))
- `steam-deck-uhid-gyro-reset.service` — oneshot unit that clears Steam's virtual gamepad registry at boot (installed & enabled by install.sh) so the deck controller re-registers with IMU/gyro initialized after Bazzite updates
- `Agent.md` — full debugging log: hypotheses, measurements, reproduction steps
- `steam-input-ref.png` — Steam Input reference screenshot
- `SOURCE_BASE_COMMIT` / `SOURCE_UPSTREAM` — exact upstream source used
- `SHA256SUMS`, `NOTICE.md`, `LICENSE`

## Disclaimer

Unofficial and experimental. Not affiliated with Lenovo, Valve, SteamOS, Bazzite, or the InputPlumber maintainers. Use at your own risk.

## License

Contains a modified InputPlumber build. Distributed under the GNU General Public License version 3 or later. See `LICENSE`.

## Suspend / Resume fixes (included in this patch)

This patch also ships two power-management fixes for the Legion Go 2 on Bazzite (both user-verified working on the live system).

### 1. Sleep fix — the device can sleep and no longer wakes from the controllers

The virtual Steam Deck controller is attached over `vhci_hcd`/usbip. An active usbip connection makes the kernel refuse suspend (`vhci_hcd: We have 1 active connection. Do not suspend.` → instant wake). The suspend hook unit

```
/usr/lib/systemd/system/inputplumber-suspend.service
```

drops that connection before sleep (`ExecStart=... HookSleep`) and re-creates the controller on wake (`ExecStop=... HookWake`). Enable it once:

```bash
sudo systemctl enable --now inputplumber-suspend.service
```

The suspend side (`ExecStart`/`HookSleep`) is left byte-identical to upstream.

### 2. Resume fix — the virtual Steam Controller actually returns to Steam after wake

**Problem — the root cause.** After suspend → resume, the virtual Steam Controller (`deck`, exposed via vhci) was torn down and re-created repeatedly ("churn"). The `poll()` function in `src/input/target/steam_deck.rs` unconditionally created a new `VirtualUSBDevice` and overwrote the stored device **without stopping the old one**, leaving an orphaned vhci device behind. Steam could never finish registering it, so after detach/re-attach or resume, Steam showed **no controller** at all.

**Fix.** `poll()` now **reuses the existing virtual device** when a new config arrives during a re-attach, instead of spawning a second vhci device. This stops the churn so Steam can finish registering the controller, and it reliably returns after wake.

> **Included in:** the **v6 release** binary — shipped as `inputplumber-legiongo2-gyro` in the tarball, installed as `inputplumber-legiongo2-gyro-v6`.

**Controller name note.** After updating Bazzite to **44.20260831**, the controller appears in Steam as **"Steam Deck Controller"** (instead of the older identifier). This is expected — it is still the same `deck` target exposed over vhci.

### 3. Steam virtual gamepad registry — IMU not initialized (dead gyro)

If Steam registers the `deck` controller **without initializing its IMU**, the gyro is dead and the Steam sensitivity sliders sit at **0**.

- File: `~/.local/share/Steam/config/virtualgamepadinfo.txt`
- **Fix:** close Steam completely, delete that file, then start Steam so the controller re-registers **with the IMU initialized**:

```bash
rm -f ~/.local/share/Steam/config/virtualgamepadinfo.txt
```

### 4. Auto-reset after updates (installed by install.sh)

A Bazzite (bootc/rpm-ostree) update can make Steam re-register the virtual Steam Deck controller **without initializing its IMU** — the same dead-gyro symptom as [section 3](#3-steam-virtual-gamepad-registry--imu-not-initialized-dead-gyro), but triggered automatically by the update instead of by a suspend/resume cycle.

**install.sh** installs a small oneshot systemd unit, [`steam-deck-uhid-gyro-reset.service`](steam-deck-uhid-gyro-reset.service), that runs at every boot **before the graphical session / Steam starts**. It runs as the real desktop user and simply deletes Steam's virtual gamepad registry entry:

```bash
rm -f ~/.local/share/Steam/config/virtualgamepadinfo.txt
```

Deleting a missing file is a no-op (`rm -f`), so the unit is safe and idempotent — when there is no stale entry it just exits. If an update left a stale entry (gyro dead / sliders stuck at 0 / controller shown as **"SteamOS Handheld Controller"** instead of **"Steam Deck Controller"**), the file is already gone by the time Steam starts, so Steam re-registers the controller **with the IMU initialized** and the gyro works.

The unit is enabled automatically by install.sh (`systemctl enable --now steam-deck-uhid-gyro-reset.service`) — no action is needed after updates.
