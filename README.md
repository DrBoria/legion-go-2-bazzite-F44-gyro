# legion-go-2-bazzite-F44-gyro

Gyro fix for the **Lenovo Legion Go 2** on **Bazzite (Fedora 44)**, based on a modified [InputPlumber](https://github.com/ShadowBlip/InputPlumber) build from source.

**v13** rebases the fix onto upstream **InputPlumber v0.79.1** (previous releases were based on v0.77.4). Upstream's Go1/Go2 driver split, its own Legion Go 2 suspend handling and the Steam Deck debounce removal are adopted; our gyro calibration, live attach/detach, source arbiter and suspend/deck-feed hardening are kept on top.

## What it fixes

- Controllers **attached** → the **central** gyro is active.
- Controllers **detached** → only the **right-handle** gyro is active.

Upstream mixes the sensors incorrectly; this build makes the central gyro work when the controllers are attached and keeps only the right-handle gyro when they are detached.

## Steam Input: gyro must be "Always On"

Open the Steam overlay → **Gyro Behavior** → gear icon → **General / choose gyro button(s)** → **None (Gyro Always On)**. By default the gyro only works while a button is held, which looks dead.

![Steam Input gyro — set to None (Gyro Always On)](steam-input-ref.png)

## Install

Download **`inputplumber-legiongo2-gyro-v13.tar.gz`** from the [Releases](https://github.com/DrBoria/legion-go-2-bazzite-F44-gyro/releases) page, extract it and run as your regular user (sudo is used internally):

```bash
tar xzf inputplumber-legiongo2-gyro-v13.tar.gz
./install.sh        # binary + profile + gains + suspend/resume fixes + gyro auto-reset
./install.sh --log  # same, plus installs & enables the diagnostic logger
```

`install.sh` installs the modified binary, the composite device profile that routes the device to the gyro-capable Steam Deck target, comfortable gyro gains, the suspend/resume power fix and a boot-time Steam gyro auto-reset unit.

## Diagnostic logger (`--log`)

The optional logger is one command: `./install.sh --log` captures everything into a single file, `/var/log/ip-gyro-logger.log` — just send that file back for support. It tracks delivery hop-by-hop (H0 physical → H1 deck → Steam → H2 Steam config → H3 virtual pad → game) per data type (buttons / sticks / gyro), so the first hop where input stops is easy to spot.

## Reference log

`ip-gyro-logger.log` in this repository is the reference session recorded in the accompanying video, stored byte-for-byte as-is — do not open it.

## Files

- `install.sh` — the installer (two modes: plain / `--log`)
- `50-legion_go_2.yaml` — composite device profile (Steam Deck target with gyro)
- `logger/` — the diagnostic logger sources (`ip-gyro-logger.py` + `ipgyro/` package)
- `patches/inputplumber-legion-go-2-bazzite.patch` — the complete source patch vs upstream
- `Agent.md` — full debugging notes (hypotheses, measurements, reproduction)
- `SHA256SUMS` / `SOURCE_BASE_COMMIT` / `SOURCE_UPSTREAM` / `NOTICE.md` / `LICENSE`

## Build from source

The patch applies on top of upstream **InputPlumber v0.79.1** (see `SOURCE_BASE_COMMIT`):

```bash
git clone https://github.com/ShadowBlip/InputPlumber
cd InputPlumber
git checkout 0ca9869   # v0.79.1
git apply ../legion-go-2-bazzite-F44-gyro/patches/inputplumber-legion-go-2-bazzite.patch
cargo build --release
# result: target/release/inputplumber
```

Unofficial and experimental. Not affiliated with Lenovo, Valve, SteamOS, Bazzite, or the InputPlumber maintainers. Use at your own risk.
