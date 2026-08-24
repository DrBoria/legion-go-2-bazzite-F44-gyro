л# legion-go-2-bazzite-F44-gyro

Gyroscope fix for the **Lenovo Legion Go 2** on **Bazzite (Fedora 44)**, based on a modified [InputPlumber](https://github.com/ShadowBlip/InputPlumber) build from source.

## What it fixes

- **Controllers attached** → the **central** gyroscope is active.
- **Controllers detached** → only the **right-handle** gyroscope is active.

Upstream exposes a single gyro source and mixes the center/right sensors incorrectly. This build makes the central gyro work when the controllers are attached and keeps only the right-handle gyro when they are detached.

## Install (prebuilt binary)

Download **`inputplumber-legiongo2-gyro-v5.tar.gz`** from the **Releases** page, extract it and run:

```bash
tar xzf inputplumber-legiongo2-gyro-v5.tar.gz
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
sudo cp inputplumber-legiongo2-gyro /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v5
sudo chmod +x /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v5

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

> **⚠️ Steam Input reference** — see the screenshot below. **Don't forget to turn this setting off.**
>
> ![steam-input-ref](steam-input-ref.png)

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
- `inputplumber-legiongo2-gyro` — prebuilt modified binary (Release asset: `inputplumber-legiongo2-gyro-v5.tar.gz`)
- `50-legion_go_2.yaml` — composite device profile (routes the device to the `deck` target so it is seen as a Steam Deck with gyro)
- `install.sh` — install / update script (binary + profile + gain override)
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

### 2. Resume fix — joysticks / touchpads return to Steam after wake

On resume `HookWake` re-creates the virtual Steam Deck controller (28de:1205) but did **not** re-trigger udev, so Steam (which ran through the whole suspend) never re-detected it — the controller was missing from Steam until a manual restart. This patch bakes a udev re-scan into the hook's `ExecStop` (resume side only):

```ini
ExecStop=/bin/bash -c 'busctl call org.shadowblip.InputPlumber /org/shadowblip/InputPlumber/Manager org.shadowblip.InputManager HookWake; sleep 2; udevadm trigger --subsystem-match=input --subsystem-match=hidraw --subsystem-match=iio'
```

The unfiltered `udevadm trigger` (no serial filter) ensures the re-created vhci controller's `input` nodes are re-scanned so Steam re-detects it after every wake. See [`Agent.md`](Agent.md) for the full investigation and reproduction steps.
