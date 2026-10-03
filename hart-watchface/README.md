# hart watch face

A Connect IQ watch face for the Forerunner 965 in the hart web UI's style: Playfair Display time,
Body Battery and watch-battery arcs, heart rate, steps and a countdown to race day. In always-on mode it
draws outlined digits that drift a few pixels each minute (AMOLED burn-in rules).

Phase 1 uses on-watch data only. Fetching readiness, phase and form from the hart server
(phone → Tailscale → `/api/watch`) is phase 2.

## Build

Needs the Connect IQ SDK (SDK Manager) and a developer key at `hart-watchface/developer_key`
(gitignored — keep a backup; sideloaded builds must be signed with the same key to update).

```bash
SDK="$(cat ~/Library/Application\ Support/Garmin/ConnectIQ/current-sdk.cfg)"
"$SDK/bin/monkeyc" -f monkey.jungle -d fr965 -y developer_key -o bin/hart.prg
"$SDK/bin/connectiq" &                     # simulator
"$SDK/bin/monkeydo" bin/hart.prg fr965
```

The simulator keeps app settings between runs; reset them under File → Edit Persistent Storage.

## Install

Connect the watch over USB and copy `bin/hart.prg` to `GARMIN/APPS/` (on macOS use OpenMTP or
Android File Transfer). Then pick **hart** under Watch Face on the watch.

## Settings

The race date (`YYYY-MM-DD`) is set in the Connect IQ phone app (or Garmin Connect →
device → watch face settings). The countdown ("323 days to go") is hidden until a date is set or after it passes.
Sideloaded faces may not show settings in the phone app; if so, use the simulator's
File → Edit Application.Properties to check it; phase 2 can take the date from hart instead.

## Fonts and icon

`resources/fonts/*` and `resources/drawables/launcher_icon.png` are generated from the web UI's
fonts by `tools/make_assets.py` (command in its docstring).
