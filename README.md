# xrandrw

[![PyPI version](https://img.shields.io/pypi/v/xrandrw.svg)](https://pypi.org/project/xrandrw/)
[![Python versions](https://img.shields.io/pypi/pyversions/xrandrw.svg)](https://pypi.org/project/xrandrw/)
[![License: MIT](https://img.shields.io/pypi/l/xrandrw.svg)](https://github.com/kleinpanic/xrandrw/blob/main/LICENSE)

Zero-config, self-healing X11 display-layout daemon for dwm/i3 and other
bare-X11 window managers. It watches for monitor hotplug/unplug, remembers each
display by its EDID identity, and auto-places externals relative to a primary
using a persistent attach-order policy — so your monitors end up where you left
them, every time.

One runtime dependency: [`python-xlib`](https://pypi.org/project/python-xlib/)
(≥ 0.31), used to talk to X11 and RandR directly rather than scraping subprocess
output. Everything else is the standard library. Optional extras:
`xrandrw[journald]` for `systemd-python` journald logging, `xrandrw[wallpaper]`
for the pure-Python Pillow wallpaper backend.

## Scope

**X11 only — there is no Wayland support and none is planned.** xrandrw drives
RandR directly; under a Wayland compositor it has nothing to talk to.

It is built for **bare window managers** (dwm, i3, bspwm, awesome, and similar)
that have no display-configuration daemon of their own. **It will fight a full
desktop environment.** GNOME (mutter) and KDE (KScreen) run their own output
managers that reassert their saved layout, so the two will overwrite each other
in a loop. Do not run xrandrw alongside them without first disabling their
display management.

## Requirements

Beyond Python ≥ 3.9 and a running X server:

| Binary | Needed | Debian/Ubuntu | Arch |
|--------|--------|---------------|------|
| `xrandr` | **Required** — applying any layout. | `x11-xserver-utils` | `xorg-xrandr` |
| `xset` | **Required** — polling for X readiness at startup. | `x11-xserver-utils` | `xorg-xset` |
| `xinput` | Only if you set `TOUCH_MAP`. | `xinput` | `xorg-xinput` |
| `feh` | Optional wallpaper backend. | `feh` | `feh` |
| `xwallpaper` | Optional wallpaper backend. | `xwallpaper` | `xwallpaper` |

```bash
# Debian/Ubuntu — required only
sudo apt install x11-xserver-utils
# Arch — required only
sudo pacman -S xorg-xrandr xorg-xset
```

A missing **optional** binary is a **silent no-op**, not an error: with no
wallpaper backend installed, layout changes still apply and the wallpaper simply
is not reset. Likewise a `TOUCH_MAP` set without `xinput` present remaps
nothing. Run with `LOG_LEVEL=debug` if a feature seems inert.

(`fehbg` is a third-party helper script rather than a distro package. It is
auto-detected when on `PATH`, and it ignores `WALL` — see the `WALL` row in
[Configuration](#configuration).)

## Install

End-user (isolated, lands the `xrandrw` console-script at `~/.local/bin` — never
touches system Python):

```bash
pipx install xrandrw            # from PyPI
# or, from a checkout: pipx install .
```

Optional journald logging extra:

```bash
pipx install "xrandrw[journald]"
```

Development — always in a virtualenv, never a bare/root `pip`:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"         # editable + ruff, vulture, pytest, build
```

## Usage

`xrandrw` exposes seven modes:

| Command | Description |
|---------|-------------|
| `xrandrw --apply` | Apply the layout once (default when no flag is given). |
| `xrandrw --watch` | Event-driven watch: re-apply on RandR hotplug notifications. |
| `xrandrw --daemon` | The same event-driven watch, plus the systemd `sd_notify` readiness handshake (the intended service entry point). |
| `xrandrw --print` | Print `xrandr --query` output and exit. |
| `xrandrw --set-pref OUTPUT_OR_ID SIDE` | Set a display's preferred side: `right-of`, `left-of`, `above`, `below`. |
| `xrandrw --list-state` | Dump the placement state JSON. |
| `xrandrw --window-state` | Print a JSON diagnostic of the window-mgmt feature state — enabled, dwm-ipc availability, and captured windows. |

**`--watch` and `--daemon` run the identical loop.** Both register for RandR
`ScreenChange`/`OutputChange`/`CrtcChange` notifications and block in `select()`
on the X connection — neither one busy-polls. The only differences are that
`--daemon` emits `sd_notify("READY=1")` for systemd `Type=notify` readiness and
logs a `daemon_start` line. Use `--daemon` under systemd and `--watch` when
running it by hand; the display behaviour is the same. (`POLL_INTERVAL` is the
`select()` timeout in both — a slow safety net, not the detection mechanism.)

## Configuration

Config resolves from (lowest to highest precedence): built-in defaults →
`/etc/xdg/xrandrw.conf` → `~/.config/xrandrw.conf` → process environment.

Every key in the table below can be set in a config file (`KEY=value` lines) or
via an environment variable of the same name. **`LAYOUT_*` is the exception: it
is config-file-only**, because the environment overlay iterates a fixed key list
that cannot enumerate arbitrary profile names.

**Config files are not shell scripts.** They are parsed as plain `KEY=value`
lines; surrounding quotes are stripped and nothing else is interpreted. `$HOME`,
`${XDG_DATA_HOME}`, `~` and command substitution are **not expanded** — a `WALL`
of `"$HOME/wall.jpg"` is stored as that literal string and never resolves. Write
absolute paths in full.

| Key | Purpose |
|-----|---------|
| `USE_XWALLPAPER` | `0` = feh/fehbg, `1` = xwallpaper. |
| `WALL` | Wallpaper image path. **Not honoured by the `fehbg` backend** — `fehbg` is a third-party script that picks its own image and takes no path argument, so `WALL` is a no-op there (logged as `wallpaper_wall_ignored`). Set `WALLPAPER_ENGINE=feh` (or `xwallpaper`/`native`) to have `WALL` applied. |
| `HIDPI_WIDTH` | Treat internal panel as HiDPI when preferred-mode width ≥ this. |
| `POLL_INTERVAL` | Slow safety-net `select()` timeout in seconds (default `45`). **Not the hotplug detection mechanism** — detection is event-driven via RandR, and this is only the fallback wakeup for the rare case an event is missed. Lowering it does *not* make hotplug faster; it just wastes CPU. |
| `LOG_LEVEL` | `none`, `err`, `info`, `notice`, `debug`. |
| `LOG_FILE` | Optional JSON-lines log file (unset = journald/stderr). |
| `LOCKFILE` | Apply-lock path. **Defaults to `$XDG_RUNTIME_DIR/xrandrw.lock`**, falling back to `/run/user/$UID/` and then `~/.local/share/xrandrw/`. Deliberately never world-writable `/tmp`, so another local user cannot pre-create or squat the lock. Set it explicitly only if you have a reason to. |
| `STATE_LOCKFILE` | Not user-settable — derived alongside `LOCKFILE` in the same per-user runtime directory (`xrandrw.state.lock`) and listed here only so it is not a surprise in a directory listing. |
| `PREF_DEFAULT_SIDE` | Default side for new/unknown monitors. |
| `EXCESS_WINDOW_SEC` / `EXCESS_THRESHOLD` | Churn-backoff window and threshold. |
| `TOUCH_MAP` | Map touch/stylus devices to outputs, e.g. `"ELAN Touchscreen:eDP-1"`; `;`-separate multiple. |
| `WALLPAPER_ENGINE` | `feh`, `fehbg`, `xwallpaper`, `native`, or empty for auto-detect (default). Unknown values fall back to auto-detect. |
| `APPLY_BACKEND` | `subprocess` (default). `native` is a seam-stub that warns and delegates to `subprocess` — not yet a real native path. |
| `LAYOUT_*` | Named device profile — a fixed `xrandr` layout for an exact set of connectors. |
| `WINDOW_MANAGEMENT` | Opt-in dwm-ipc window relocation on hotplug (`0` = off default, `1` = on). See [Window management (dwm-ipc)](#window-management-dwm-ipc). |
| `BOUNCE_SUSPECT_MS` | A disconnect arriving this soon after an apply is treated as a possible replug bounce rather than a genuine unplug (default `5000`). |
| `BOUNCE_HOLDDOWN_MS` | How long to keep re-reading the topology before believing a suspect disconnect (default `3000`). `0` disables the hold-down entirely. |

See [`xrandrw.conf.sample`](https://github.com/kleinpanic/xrandrw/blob/main/xrandrw.conf.sample)
for an annotated template —
copy it to `~/.config/xrandrw.conf` and edit.

### Device profiles (`LAYOUT_*`)

When the generic attach-order policy is not what you want, `LAYOUT_<NAME>` pins
an exact layout for an exact set of connectors and overrides the policy
entirely.

```ini
# Laptop panel alone.
LAYOUT_SOLO="eDP-1:auto:primary:0x0"

# Laptop panel + desk monitor: 2560x1440 external above the internal panel.
LAYOUT_DESK="eDP-1:auto:primary:0x0;HDMI-1:2560x1440:secondary:above=eDP-1"
```

With only the laptop open, `LAYOUT_SOLO` fires. Plug in the desk monitor and
`LAYOUT_DESK` fires instead.

**Matching is exact set equality, not subset.** A profile fires only when the
connected connector set *equals* its own. `LAYOUT_SOLO` above does **not** fire
when `HDMI-1` is also connected — which is why the two-head case needs its own
profile. (Subset matching was removed as a defect: a one-connector profile would
win on a two-head topology and leave the second head unconfigured and black.)
If two profiles name the identical connector set, the alphabetically-first
`LAYOUT_` name wins, so ties are deterministic rather than config-order
dependent.

Grammar — `;`-separated outputs, `:`-separated fields:

```
connector:mode:role:position[:transform...]
```

| Field | Values |
|-------|--------|
| `connector` | Connector name, e.g. `eDP-1`, `HDMI-1`, `DSI-1`. |
| `mode` | An xrandr mode such as `1920x1080`, or `auto` for `--auto`. |
| `role` | `primary` or `secondary`. |
| `position` | Absolute `XxY` (e.g. `1600x0`), or relative `side=CONNECTOR` (e.g. `right-of=eDP-1`) where side is `left-of`/`right-of`/`above`/`below`. |
| `transform` | Optional and repeatable: `scale=WxH`, `rotate=left\|right\|inverted\|normal`. |

A malformed `LAYOUT_` line is skipped with a log line rather than crashing the
daemon — so a typo silently disables that one profile. Check the journal if a
profile is not firing.

### Replug bounce hold-down

A physical replug rarely presents one clean connect edge — the connector
typically drops **again** a moment after coming back. Taking that second drop at
face value powers the head off, so windows that had just been restored get
evacuated and then dragged back: four visible window movements for one replug
instead of two.

The hold-down closes that. When a disconnect arrives within `BOUNCE_SUSPECT_MS`
of the previous apply, the daemon treats it as *suspect* and re-reads the
topology for up to `BOUNCE_HOLDDOWN_MS` before acting. If the connector returns
within that window, the whole off/on cycle is suppressed and no window moves.

The wait is deliberately **asymmetric**: only the **disconnect** edge is held.
Connect/replug edges are never delayed, and a **genuine unplug** — one arriving
long after the previous apply, so outside the suspect window — is healed
immediately with **no added latency**. That gating is the point: it keeps the
common case fast while still absorbing the rare bounce.

Raise `BOUNCE_HOLDDOWN_MS` if your cable still produces a double cycle; lower
`BOUNCE_SUSPECT_MS` if a real unplug ever feels sluggish; set
`BOUNCE_HOLDDOWN_MS=0` to turn the feature off.

**The cost, stated plainly.** A disconnect that *does* fall inside the suspect
window pays up to `BOUNCE_HOLDDOWN_MS` (default 3 s) of added latency before the
head is powered off — that is the trade the feature makes. And the default 3000
is **a conservative bound, not a measured bounce duration**: the actual dark
interval could not be resolved from either captured trace, because the daemon was
blocked inside its own modeset for most of it. Both traces bound it at roughly
1.7–3.0 s, so 3000 clears one comfortably and only just meets the other. Treat it
as a knob to tune on your own hardware, not a figure derived from measurement.

## Other known limitations

- **`APPLY_BACKEND=native` is a stub.** It is a seam left in place for a future
  pure-Xlib apply path. Selecting it logs a warning and delegates to the
  `subprocess` backend; it does not change behaviour. `subprocess` is the only
  real backend today.
- The bounce hold-down latency described just above.
- The window-management limitations listed under
  [Window management](#window-management-dwm-ipc), which also records that live
  hardware verification covers a single machine.

## Window management (dwm-ipc)

An opt-in feature (default off) that gives display hotplug an Apple-like feel: on
unplug, xrandrw relocates the removed display's windows onto a surviving display,
and on replug it moves the **same process's** window back where it was — keyed by
`(pid, starttime)` read from `/proc`, not by PID alone, so a recycled PID cannot
be mistaken for the original process.

**Requirement.** This needs a dwm built with the
[mihirlad55/dwm-ipc](https://github.com/mihirlad55/dwm-ipc) patch, which exposes a
control socket at `/tmp/dwm.sock`. On any window manager without that IPC endpoint
(vanilla dwm, i3, RPi4 stock dwm) the feature is **silently capability-gated off**
and your display layout is entirely unaffected — nothing to configure, nothing to
break.

**Enable it** by setting `WINDOW_MANAGEMENT=1` (env or config file). Effective-on
is `WINDOW_MANAGEMENT=1` **and** a live dwm-ipc endpoint; either missing means the
subsystem is a no-op.

**Inspect it** with the read-only diagnostic:

```bash
xrandrw --window-state
```

It always exits 0 — when the feature is off, when no endpoint is present, and
even when the capture itself fails (never a traceback). The JSON keys are:

| Key | Type | Meaning |
|-----|------|---------|
| `enabled` | bool | Whether `WINDOW_MANAGEMENT=1` is set. |
| `dwmipc_available` | bool | Whether a usable dwm-ipc socket was found. |
| `captured` | list | Window records read live from dwm. Empty unless **both** flags above are true. |
| `displaced` | list | **Always empty here** — see below. |
| `reason` | string | Present **only** on a degraded path, explaining which of the two gates failed. Absent when the feature is fully live. |

**`displaced` is always `[]` from the CLI.** Displaced records — the windows
evacuated from an unplugged output, awaiting its return — exist only in the
memory of the *running daemon's* relocation coordinator. `--window-state` is a
separate one-shot process with no coordinator of its own and no way to interrogate
another process's, so it has nothing to report. The key is emitted regardless to
keep the schema stable. To see relocation activity as it happens, read the
daemon's `relocate_*` log events (`LOG_LEVEL=debug`) rather than this command.

**Limitations.**

- **Fullscreen is not reapplied.** A window's fullscreen state is captured but not
  restored (Phase-10 IN-03); a formerly-fullscreen window returns to its saved
  monitor/tag/floating-state/geometry but not its fullscreen flag.
- **Cross-monitor relocation needs ≥ 2 heads** — with a single output there is
  nowhere to relocate to.
- **Monitor targeting is relative.** Restore uses dwm's relative `tagmon`, so the
  target monitor is derived from the current topology, not an absolute index.
- **Displaced windows are remembered by connector, not by monitor identity.** If
  you unplug a monitor and then plug a *different* monitor into the same port,
  the first monitor's windows are restored onto the new one. Windows are keyed by
  connector name (`HDMI-1`), and although each record captures the monitor's EDID
  it is not currently consulted on restore.
- **Mirrored outputs get no mapping, so relocation silently does nothing there.**
  Two outputs both connected at identical position and mode are genuinely
  ambiguous, and the dwm-monitor→connector matcher refuses to guess: it returns
  `None` rather than binding arbitrarily. Records carrying `output=None` are never
  eligible for displacement, so on a mirrored setup the feature is inert. This is
  a deliberate refusal — guessing would risk moving windows to the wrong head —
  but it is silent apart from a `window_monitor_unmatched` log line.

**Verification scope.** The unplug/replug behaviour above has been confirmed by
physical hardware testing on **exactly one machine** — a Dell laptop, `eDP-1` +
`HDMI-1`, dwm 6.5 with the dwm-ipc patch. Everything else is covered by automated
tests only. Other hardware, docks, and three-or-more-head configurations are
plausible but unproven; please report what you find.

### Adoptability & graceful degradation

xrandrw's **core daemon** — hotplug detection, layout/placement, EDID identity,
wallpaper reapply, and touch remap — is **generic X11** and runs on **any bare-X11
window manager** with no window-management dependency. The window-management
relocate/restore feature is a **strictly additive** layer that **requires** a dwm
built with the [mihirlad55/dwm-ipc](https://github.com/mihirlad55/dwm-ipc) patch
exposing `/tmp/dwm.sock`.

On **any WM without that socket** — vanilla dwm, i3, a stock Raspberry Pi 4 dwm —
the feature is **silently capability-gated off** and the display-layout daemon is
**completely unaffected**: no dwm-ipc call is ever issued, no window is ever moved,
and a machine with `WINDOW_MANAGEMENT` unset never even probes the socket. So
adopting xrandrw on a bare-X11 box costs nothing and breaks nothing.

This contract is not incidental — a dedicated **anti-regression CI suite**
(`.github/workflows/regression.yml`, REG-01) runs the gate-off / RPi4-style path
**with no dwm-ipc socket present** and fails the build if any future change ever
made the gated-off window-mgmt path run (issue a dwm-ipc call or move a window) on
a no-endpoint machine. The RPi4 / vanilla-dwm path can therefore never silently
break as the window-mgmt code evolves.

## Testing & quality gates

The suite runs in **two tiers**, split by the `functional` pytest marker.

**Display-free tier — `pytest -m 'not functional'`.** The default, and the bulk
of the suite. The window-management capture→restore lifecycle is driven against a
**real `AF_UNIX` fake dwm-ipc server** speaking the DWM-IPC wire protocol, a
**mocked Xlib** seam, and a **fake `/proc`**. None of it needs a display, so it
runs headless with no X server, no dwm, and no second monitor.

**Real-dwm tier — `pytest -m functional`.** The opposite: it needs a display and a
window manager. CI's `functional` job installs `xvfb` and `xserver-xephyr`, builds
a **real dwm** from a vendored checksum-pinned `dwm-ipc.diff`, and runs the suite
against it over a private socket (Xephyr nested in Xvfb to get two Xinerama heads).
This tier is **new and barely exercised** — it first went green on 2026-07-19 and
has only a handful of passing runs, so it has not yet protected anything
historically. Treat it as a gate going forward, not as retrospective evidence.

Neither tier can prove the real thing. No CI runner can flip a connector's
hotplug-detect bit, so the true unplug→relocate→replug→restore chain is verified
only by a physical hardware run, recorded in `tests/functional/L3_PASS.stamp` and
gated on at release time.

CI (`ci.yml`) runs **nine** jobs — eight gating, one advisory:

- **`test`** — `pytest -q` plus `python -m build` across Python 3.9 / 3.11 / 3.13.
- **`coverage`** — **two** separate runs: a line-coverage gate of **95** scoped to
  `xrandrw.dwmipc` / `xrandrw.windows` / `xrandrw.relocate`, and a whole-package
  `--cov-branch --cov-fail-under=85` ratchet.
- **`lint`** — baseline `ruff check .`.
- **`lint-strict`** — the expanded `B,SIM,PERF,C90,UP,RUF` ruleset across **all** of
  `src/xrandrw/`.
- **`dupcode`** — `pylint` duplicate-code (R0801) at `--min-similarity-lines=6`.
- **`deadcode`** — `vulture` over `src/`.
- **`cli-smoke`** — `pip install .` (a real, non-editable install) then invokes the
  installed `xrandrw` console-script, catching entry-point regressions.
- **`functional`** — the real-dwm tier described above.
- **`functional-vkms`** *(advisory, `continue-on-error`)* — a best-effort real
  Xorg + dummy-driver / VKMS probe. It documents that a true output-status flip is
  still not reachable headlessly; it never gates.

Two further workflows sit outside `ci.yml`: **`regression.yml`** (the no-dwm-ipc
degradation gate, on every push/PR) and **`mutation.yml`** (a nightly mutation-testing
ratchet, also dispatchable on demand).

## systemd user service

Running `xrandrw --daemon` as a `--user` service (with `Restart=always` and
`sd_notify` integration) is the intended deployment. There are two paths.

### From a plain `pipx install` (no repo checkout)

The wheel on PyPI does **not** ship the unit file — a wheel unpacks into
`site-packages`, where `systemctl --user` cannot reach it. Write it yourself:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/xrandrw.service <<'EOF'
[Unit]
Description=xrandrw: automatic X11 display hotplug policy (dwm)
# Session helper: enabling this links it under dwm-session.target.wants/ and it
# stops with the session. Generic (non-dwm) setups can retarget both lines to
# graphical-session.target.
After=dwm-session.target
PartOf=dwm-session.target
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin
Environment=WALL=%h/.local/share/backgrounds/space.jpg
Environment=LOG_LEVEL=notice
# Environment=LOG_FILE=%h/.local/state/xrandrw/xrandrw.log
# Optional: USE_XWALLPAPER=1 ; HIDPI_WIDTH=3200 ; PREF_DEFAULT_SIDE=right-of

ExecStart=%h/.local/bin/xrandrw --daemon
Restart=always
RestartSec=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=dwm-session.target
EOF
```

**Read [Choosing the right `WantedBy` target](#choosing-the-right-wantedby-target)
before enabling it** — the default target only exists on the author's machine.

Adjust `WALL=` to a wallpaper you actually have (or delete the line). `pipx`
places the console-script at `~/.local/bin/xrandrw`, which is exactly the
`ExecStart` path above.

### From a repo checkout

```bash
make install    # pipx install . + copy conf sample + unit
make enable     # systemctl --user daemon-reload && enable --now
```

Run `systemctl --user daemon-reload` after any reinstall.

### Choosing the right `WantedBy` target

**If your session is not driven by a `dwm-session.target`, you must retarget the
unit — otherwise it will stop starting after your next reboot, with no error.**

This is a silent failure, which is why it gets its own section. The shipped unit
uses `dwm-session.target` in `After=`, `PartOf=`, and `WantedBy=`. That target is
a personal session unit which this repo does **not** install. `systemctl --user
enable` still succeeds against a non-existent target, and `--now` still starts
the service, so everything looks correct — until the next boot, when nothing
ever pulls the unit in.

If you do not have a `dwm-session.target`, replace all three references before
enabling:

```ini
After=graphical-session.target
PartOf=graphical-session.target

[Install]
WantedBy=graphical-session.target
```

Or, against an already-installed unit:

```bash
sed -i 's/dwm-session\.target/graphical-session.target/g' \
  ~/.config/systemd/user/xrandrw.service
systemctl --user daemon-reload
systemctl --user reenable xrandrw.service
```

(`reenable` is required — the old symlink still points at the old target.)

List the targets your session actually provides with:

```bash
systemctl --user list-units --type=target
```

Note that `graphical-session.target` is only reached if something in your
session reaches it; bare WM setups launched from `.xinitrc` often do not. If it
is absent, either have your session run `systemctl --user start
graphical-session.target`, or use `default.target` instead.

## Uninstall

```bash
systemctl --user disable --now xrandrw     # stop it and unlink from the target
rm -f ~/.config/systemd/user/xrandrw.service
systemctl --user daemon-reload
pipx uninstall xrandrw
```

That leaves no processes behind, but does not remove your data. To also drop the
persistent state — the EDID-to-profile identity map and attach-order stack:

```bash
rm -rf ~/.local/share/xrandrw          # state.json (honours XDG_DATA_HOME)
rm -f  ~/.config/xrandrw.conf          # your config, if you made one
```

Deleting `state.json` alone is also the way to **reset placement** without
uninstalling: the daemon rebuilds it from scratch on the next apply, forgetting
every remembered monitor and side preference. The lock files live in
`$XDG_RUNTIME_DIR` and are cleared on reboot, so they never need cleaning up.

From a repo checkout, `make uninstall` does the first block for you.

## License

MIT — see [LICENSE](https://github.com/kleinpanic/xrandrw/blob/main/LICENSE).
