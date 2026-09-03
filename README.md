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

## What's new in v9

**v9** is the current release — the **sleep-in-game fix**. It ships the `c9a4bfa8` build
(prebuilt binary sha256:
`c9a4bfa800a2c1bca078c41ddfcb0131351cd8f5402d8a5cdd4963ca13476e00`) together with the
**v3.1 diagnostic logger**.

### 1. Sleep-in-game fix — the virtual Steam Controller survives system sleep

**Problem.** Sleeping *while a game is running* killed the controller. On suspend,
InputPlumber's suspend handler **stopped and removed every target device** — including the
virtual Steam Controller (`deck-uhid`, `28de:12f0`) that Steam's running-game session is
bound to. The game kept its now-dead Steam Input session: after wake Steam logged
`Controller device closed after hid_read failure`, the running game never re-bound, and the
controller stayed dead in-game until a restart. This is exactly what the log showed on
**2026-09-03 10:33:08**, before this fix.

**Fix** ([`src/input/composite_device/targets.rs`](src/input/composite_device/targets.rs),
`handle_suspend`): when the target device is the **`deck-uhid`** virtual Steam Controller it
is now **kept alive** across the suspend instead of being torn down — it is recorded in the
suspended set but **not stopped and not removed** (`continue`; no `stop()`, no removal from
`target_devices`). Every other target (keyboard, mouse) still suspends as before, and
`handle_resume` restores all of them on wake. Net effect: the physical controllers detach on
sleep as usual, but the virtual Steam Controller that the game session is using never goes
away → the Steam Input session survives sleep/wake and the **running game keeps accepting
input**.

**Verified on-device** (2026-09-03, in-game ESO sleep test): sleep at 12:12:18 → the physical
Legion controllers detached/re-attached normally, while the virtual deck got a `DEVCHG` (it
was **not** removed), InputPlumber reported `Target device deck-uhid already running, nothing
to do` right after wake (the deck was never torn down or re-created), Steam reloaded the
running game's config (`Loaded Config ... App ID 306130 ... controller_steamos_handheld.vdf`),
there was **no** Steam `hid_read failure`, and input to the **same** running-game session was
alive after wake — **confirmed by the user**.

> **Honest caveat.** The explicit keep-alive log line of `handle_suspend` only appears when
> the handler runs to completion; a suspend freeze can cut the loop short right after the
> first target is stopped. In the 12:12:18 run the log ends immediately after
> `Target device stopping/stopped: mouse0` (no final `Target devices before suspend:` marker),
> so that one explicit line was not captured. The functional proof above — deck `already
> running` after wake, no virtual-device removal, no `hid_read failure`, game config reloaded
> — is what demonstrates the deck was not torn down, and an earlier run of the same
> `c9a4bfa8` build (23:51:15, PID 1332) already logged the keep-alive line explicitly.

### 2. Diagnostic logger v3.1

Still one command (`./install.sh --log`) and one artifact (`/var/log/ip-gyro-logger.log`).
New in **v3.1**:

- **per-app Steam Input activation** capture from Steam's `controller_ui.txt` — which AppID
  Steam applies a controller config to, and when, so a "game started but the controller is
  not bound" case is visible as data instead of guesswork;
- **running-game tracker** — which Steam-launched game process is active (AppID) and what
  config Steam Input loaded for it.

These are the two markers that let the sleep failure be diagnosed from the log alone: Steam's
`Controller device closed after hid_read failure` and the per-app config reload on wake are
now recorded, not inferred.

### 3. What stayed in the binary (v8.1-era baseline)

- gaming-mode `deck-uhid` registers as **`28de-12f0`** (full buttons + gyro);
- 1000 µs poll rate (InputPlumber-added latency ~1–1.5 ms);
- **FIX C** — overlay-profile reload dedup (proven not to affect Steam activation);
- **no** destructive self-heal (removed in v8.8) — nothing in this build kills and re-creates
  the deck mid-game.

## What's new in v8.1

**v8.1** was the previous release (superseded by [v9](#whats-new-in-v9)) — a **logger-only**
patch on top of v8. The InputPlumber binary and every v8 fix were unchanged (prebuilt binary
sha256: `0618564a6194f89ca8039f4db56996ac43e05c05a23f2f80d03bbea2022689ca`).

The diagnostic logger is now the **v2** logger, and **ONE command** — `./install.sh --log`
— captures **everything** into a single log, `/var/log/ip-gyro-logger.log`, with no second
command and no extra artifact:

- IIO gyro/accel samples;
- evdev key/axis events, device inventory and udev add/remove;
- hidraw flow — raw input from the physical Legion (`LEGION-SRC` 17ef:61eb) and the reports
  INTO the virtual Steam Deck (`DECK-GAME` 28de:12f0 / `DECK-12FB` 28de:12fb /
  `DECK-DESK` 28de:1205), i.e. exactly what Steam receives;
- Steam virtual-gamepad registry + `controller.txt` verdicts (`FULL gaming deck 12f0` /
  `STALE 12fb` / `desktop Steam Controller 1205`) for **every** `/home` user (multi-home);
- InputPlumber's own journal (`IPJ` lines);
- `STATE` session/mode markers, gamescope/desktop `SESSION` transitions and a heartbeat.

After `./install.sh --log` the user only has to **reproduce the failure** and send back the
single file (`cat /var/log/ip-gyro-logger.log`) — see the
[Diagnostic logger](#diagnostic-logger-v31-optional) section below.

## What's new in v8

**v8** was the previous release (superseded by [v8.1](#whats-new-in-v81)). Prebuilt binary sha256:
`0618564a6194f89ca8039f4db56996ac43e05c05a23f2f80d03bbea2022689ca`.

### 1. Gaming-mode fix — Steam registers the full controller (`28de-12f0`, not `28de-12fb`)

**Problem.** Bazzite / OGU (Open Game UI) force the `deck-uhid` target in gaming mode. That target previously presented vendor ID `0x12fb` ("Lenovo Legion Go 2"), which Steam classified as a **"SteamOS Handheld Controller"** (`28de-12fb`) → only **A + B** worked — no joysticks, no gyro.

**Fix** ([`steam_deck_uhid.rs`](src/input/target/steam_deck_uhid.rs)): the "Lenovo Legion Go 2" branch now presents vendor **"Valve Corporation"**, name **"Steam Controller"**, `ProductId::Generic = 0x12f0`. Steam now registers **`28de-12f0`** → full buttons + gyro in gaming mode. Confirmed on-device from the logs: Steam registered `28de-12f0` (not `12fb`) during the gaming-mode `deck-uhid` windows.

### 2. Input latency optimization (POLL_RATE 2500µs → 1000µs)

`src/input/source/mod.rs` poll rate was reduced from 2500µs to 1000µs; the InputPlumber-added latency dropped to ~1–1.5 ms.

### 3. Passive diagnostic logger (two-mode install)

`install.sh` now supports **two modes**: a plain `./install.sh` (binary install only) and `./install.sh --log` (`-l` / `--logger` are aliases), which additionally installs and enables the passive [diagnostic logger](#diagnostic-logger-v31-optional). See the install section below.

## Install (prebuilt binary)

Download **`inputplumber-legiongo2-gyro-v9.tar.gz`** from the **Releases** page, extract it and run:

```bash
tar xzf inputplumber-legiongo2-gyro-v9.tar.gz
./install.sh        # plain install — binary + profile + power fixes + auto gyro-reset unit, restarts inputplumber, ensures the logger is OFF
./install.sh --log  # ONE command: same as above PLUS installs & enables the v3.1 diagnostic logger, which captures EVERYTHING into /var/log/ip-gyro-logger.log
```

> Run `install.sh` as your regular (non-root) user — it uses `sudo` internally and refuses
> to run when invoked with `sudo` itself. `./install.sh --log` is the single command for a
> full capture; afterwards only `/var/log/ip-gyro-logger.log` needs to be sent back.

`install.sh` installs **five** things, plus one **optional** sixth:

1. the modified binary to `/opt/inputplumber-legiongo2-runtime/`;
2. the composite device profile [`50-legion_go_2.yaml`](50-legion_go_2.yaml) to `/etc/inputplumber/devices.d/` — this routes the Legion Go 2 to the **`deck` (Steam Deck)** target, so Steam sees a gyro-capable controller instead of an Xbox Elite (which has no gyroscope);
3. the systemd override with comfortable gains (see below);
4. the suspend/resume power fix — enables `inputplumber-suspend.service` (so the device can sleep without waking from the controllers) and installs a drop-in that force-re-scans udev on wake so the virtual Steam Deck controller returns to Steam (see [Suspend / Resume fixes](#suspend--resume-fixes-included-in-this-patch));
5. the boot-time auto-reset unit [`steam-deck-uhid-gyro-reset.service`](steam-deck-uhid-gyro-reset.service) — clears Steam's virtual gamepad registry before Steam starts so the virtual Steam Deck controller re-registers **with its IMU/gyro initialized** after Bazzite updates (see [Auto-reset after updates](#4-auto-reset-after-updates-installed-by-installsh));
6. **optionally** the passive diagnostic logger (only with `--log`; plain `./install.sh` makes sure it is fully removed — see [Diagnostic logger](#diagnostic-logger-v31-optional)).

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

## Diagnostic logger (v3.1, optional)

In gaming mode the virtual Steam Deck controller could previously map only A/B (no joysticks, no gyro). To make such issues diagnosable after the fact, this repo ships a **passive diagnostic logger**: pure Python 3 standard library, no dependencies, no rebuild. **v9** ships the **v3.1** logger, which captures every source into a single log (`/var/log/ip-gyro-logger.log`) via the one command `./install.sh --log` — plus the new per-app Steam Input activation markers described in [What's new in v9](#2-diagnostic-logger-v31).

Files in [`logger/`](logger/):

- `logger/ip-gyro-logger.py` — records timestamped, greppable evidence: udev device add/remove events, `/proc/bus/input/devices` snapshots, IIO gyro/accel samples, evdev key/axis events, hidraw flow into the virtual deck, Steam registry verdicts, InputPlumber's journal, STATE session markers, and a heartbeat / session (gamescope/desktop) transitions. **v3.1** adds: per-app Steam Input activation from Steam's `controller_ui.txt` and a running-game tracker (AppID + loaded config).
- `logger/ip-gyro-logger.service` — systemd unit, runs as **root** (so `/dev/input/*`, `/dev/hidraw*`, `/sys/bus/iio`, every `/home` user's Steam registry and `journalctl -u inputplumber` are readable), `Restart=always`, logs to `/var/log/ip-gyro-logger.log`.

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

- `=== SNAPSHOT (device set changed) ===` + `HB: devices=N gamepads=N event_nodes=N`
  — which devices exist and are seen as gamepads;
- `UDEV DEVADD/DEVREM ...` — devices being (re)created/removed on mode switches;
- `IIO <dev> gyro=... accel=...` — the IMU is producing data;
- `EV <dev> KEY/ABS ...` — physical input is reaching the kernel;
- `SESSION ...` — gamescope/desktop session transitions;
- `HID: capturing /dev/hidrawN (LABEL vid=.... pid=....)` — logger opened a watched hidraw:
  `LEGION-SRC` (17ef:61eb, the physical controller = InputPlumber's input) and the virtual
  Steam-deck outputs `DECK-DESK` (28de:1205, desktop) / `DECK-GAME` (28de:12f0, gaming);
- `HID <label> FRAME len=N head=..` — a new frame length was seen (format signature, so
  wrong/odd report sizes become visible immediately);
- `HIDFLOW <label> N rd/s M B/s len=L gyro(p,y,r)=...` — once per second: is data flowing
  and what the virtual-deck gyro reads (signed 16-bit LE at bytes 30-35);
- `FLOW STOP <label> ...` — a gaming deck (28de:12f0/12fb) is attached but NO report reaches
  it -> the connection is being lost HERE (nothing reaching Steam);
- `FLOW IDLE <label> ...` — informational: an event-driven interface simply has nothing to send;
- `HIDFLOW RESUME <label> ...` — a source that was silent starts delivering again;
- `STATE: mode=DESKTOP|GAMING|TRANSITION ...` — current mode + which hidraws are attached;
  every Desktop -> Gaming switch and controller attach/detach logs one line;
- `IPJ: ...` — InputPlumber's own journal (61EB open/attach, gyro init, errors) tailed live;
- `STEAM: registry lines=N verdict=...` + `STEAM   | [slot N] name=.. VID=.. PID=.. ...`
  — Steam's virtual-gamepad registry for every desktop user. This is the IMU-initialization
  fingerprint: `desktop Steam Controller 1205` (fine for desktop) vs `FULL gaming deck 12f0`
  (gyro-capable, correct in gaming) vs `STALE 12fb` (A/B only, dead gyro — the failure mode);
- `STEAM: controller log present, size=N (tail-only ...)` — Steam's controller.txt tail.

A full capture for a remote report is **ONE command** (run as your normal user — NOT with
`sudo`; the script uses `sudo` internally and refuses to run as root), then just reproduce
the problem:

```bash
./install.sh --log                      # installs the patch + starts the logger service
# then reproduce the failure (~20-30s):
#   - switch Desktop -> Gaming -> Desktop a couple of times
#   - detach / re-attach the Legion controllers
#   - open a gyro game and move the device
# afterwards send back the single file (there is nothing else to collect):
cat /var/log/ip-gyro-logger.log
```

The logger is fully passive: it only reads hidraw (the kernel duplicates each report to every
open reader, so it never steals or acknowledges input) and tails InputPlumber's journal — it
cannot interfere with InputPlumber or Steam.

Steam registering **`28de-12f0`** (not `28de-12fb`) during the gaming-mode `deck-uhid` window is the expected v8/v9 behaviour.

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
- `inputplumber-legiongo2-gyro-v4.resume-gamefix` — prebuilt modified binary for **v9** (Release asset: `inputplumber-legiongo2-gyro-v9.tar.gz`); sha256 `c9a4bfa800a2c1bca078c41ddfcb0131351cd8f5402d8a5cdd4963ca13476e00`
- `inputplumber-legiongo2-gyro` — legacy prebuilt binary (v8.1-era, sha256 `0618564a6194f89ca8039f4db56996ac43e05c05a23f2f80d03bbea2022689ca`), kept as the `install.sh` last-resort fallback (no FIX A)
- `50-legion_go_2.yaml` — composite device profile (routes the device to the `deck` target so it is seen as a Steam Deck with gyro)
- `install.sh` — install / update script with two modes (plain vs `--log`) for the optional diagnostic logger (binary + profile + gain override + suspend/resume power fixes + boot-time Steam gyro auto-reset unit)
- `logger/` — passive diagnostic logger (v3.1: `ip-gyro-logger.py` + `ip-gyro-logger.service`), installed by `./install.sh --log` (see [Diagnostic logger](#diagnostic-logger-v31-optional))
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
