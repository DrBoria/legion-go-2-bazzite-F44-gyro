# legion-go-2-bazzite-F44-gyro

Gyroscope fix for the **Lenovo Legion Go 2** on **Bazzite (Fedora 44)**, based on a modified [InputPlumber](https://github.com/ShadowBlip/InputPlumber) build from source.

## What it fixes

- **Controllers attached** → the **central** gyroscope is active.
- **Controllers detached** → only the **right-handle** gyroscope is active.

Upstream exposes a single gyro source and mixes the center/right sensors incorrectly. This build makes the central gyro work when the controllers are attached and keeps only the right-handle gyro when they are detached.

## Install (prebuilt binary)

Download **`inputplumber-legiongo2-gyro-v4.tar.gz`** from the **Releases** page, extract it and run:

```bash
tar xzf inputplumber-legiongo2-gyro-v4.tar.gz
./install.sh        # asks for sudo, installs binary + profile, restarts inputplumber
```

`install.sh` installs **three** things:

1. the modified binary to `/opt/inputplumber-legiongo2-runtime/`;
2. the composite device profile [`50-legion_go_2.yaml`](50-legion_go_2.yaml) to `/etc/inputplumber/devices.d/` — this routes the Legion Go 2 to the **`deck` (Steam Deck)** target, so Steam sees a gyro-capable controller instead of an Xbox Elite (which has no gyroscope);
3. the systemd override with comfortable gains (see below).

Or manually:

```bash
sudo mkdir -p /opt/inputplumber-legiongo2-runtime
sudo cp inputplumber-legiongo2-gyro /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v4
sudo chmod +x /opt/inputplumber-legiongo2-runtime/inputplumber-legiongo2-gyro-v4

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
- `inputplumber-legiongo2-gyro` — prebuilt modified binary (Release asset: `inputplumber-legiongo2-gyro-v4.tar.gz`)
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
