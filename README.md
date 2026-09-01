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

## Install (prebuilt binary)

Download **`inputplumber-legiongo2-gyro-v6.tar.gz`** from the **Releases** page, extract it and run:

```bash
tar xzf inputplumber-legiongo2-gyro-v6.tar.gz
./install.sh        # asks for sudo, installs binary + profile + power fixes, restarts inputplumber
```

`install.sh` installs **four** things:

1. the modified binary to `/opt/inputplumber-legiongo2-runtime/`;
2. the composite device profile [`50-legion_go_2.yaml`](50-legion_go_2.yaml) to `/etc/inputplumber/devices.d/` — this routes the Legion Go 2 to the **`deck` (Steam Deck)** target, so Steam sees a gyro-capable controller instead of an Xbox Elite (which has no gyroscope);
3. the systemd override with comfortable gains (see below);
4. the suspend/resume power fix — enables `inputplumber-suspend.service` (so the device can sleep without waking from the controllers) and installs a drop-in that force-re-scans udev on wake so the virtual Steam Deck controller returns to Steam (see [Suspend / Resume fixes](#suspend--resume-fixes-included-in-this-patch)).

Or manually:

```bash
sudo mkdir -p /opt/inputplumber-legiongo2-runtime
sudo cp inputplumber-legiongo2-gyro /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v6
sudo chmod +x /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v6

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
- `inputplumber-legiongo2-gyro` — prebuilt modified binary (Release asset: `inputplumber-legiongo2-gyro-v6.tar.gz`)
- `50-legion_go_2.yaml` — composite device profile (routes the device to the `deck` target so it is seen as a Steam Deck with gyro)
- `install.sh` — install / update script (binary + profile + gain override + suspend/resume power fixes)
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
