# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-19

Adds an optional, capability-gated **window-management** subsystem: when a
display is unplugged, windows on the removed output are relocated onto the
surviving display without crashing dwm and with tiled-vs-floating state
preserved; when it is plugged back in, the *same process* (identified by
`(pid, starttime)`, never another instance) is moved back to where it was. The
feature auto-detects a patched dwm's IPC socket and silently disables itself
where absent (e.g. vanilla dwm / i3), leaving the existing display-layout
behaviour untouched. Opt-in via `WINDOW_MANAGEMENT=1`.

### Added
- `xrandrw.dwmipc` — pure-stdlib client for the mihirlad55/dwm-ipc wire protocol
  over `/tmp/dwm.sock` (no `dwm-msg` dependency), with an `available()`
  capability gate and a hardened untrusted-input boundary (size cap, per-op
  timeout with total-deadline, schema validation; every failure degrades to
  `DwmIpcUnavailable`, never a crash).
- `xrandrw.windows` — window→process identity via `_NET_WM_PID` (primary) and
  the XRes extension (fallback), keyed on `(pid, starttime)`; full per-window
  state capture (monitor, tags, floating, geometry) associated to the tracked
  output/EDID.
- `xrandrw.relocate` — the unplug-record / replug-restore lifecycle, hooked into
  the existing event-driven RandR watch loop (no new polling), reusing its
  churn-backoff to settle before issuing focus-then-act dwm-ipc commands.
- `WINDOW_MANAGEMENT` config key (default off) and a `--window-state` CLI
  diagnostic printing `{enabled, dwmipc_available, captured, displaced}` as JSON,
  plus a `reason` key on degraded paths. It always exits 0. Note that `displaced`
  is always empty from the CLI: displaced records live in the running daemon's
  coordinator, which a separate one-shot process cannot read.
- CI: a scoped line-coverage gate of 95 on the new modules plus a whole-package
  `--cov-branch --cov-fail-under=85` ratchet; the expanded `ruff` ruleset
  (`B`/`SIM`/`PERF`/`C90`/`UP`/`RUF`) across all of `src/xrandrw/`; `pylint`
  duplicate-code and `vulture` dead-code jobs; a `cli-smoke` job that invokes the
  really-installed console-script; a display-free suite (fake `AF_UNIX` dwm-ipc
  server + mocked Xlib + fake `/proc`) needing no X server or hardware; a
  real-dwm functional tier built from a vendored dwm-ipc patch under
  Xephyr-in-Xvfb; a no-dwm-ipc anti-regression workflow; and a nightly mutation
  ratchet.
- `SECURITY.md` documenting the local-desktop threat posture and accepted risks.

### Fixed
- Windows now reliably return to the external display after an unplug/replug;
  previously they could be stranded on the internal panel. This needed three
  linked fixes: the dwm-monitor↔RandR-output matcher now recognises an
  unplugged-but-still-lit output (via a connected-*preferring* tie-break, so a
  genuine mirrored pair is unaffected and still refuses to guess), `scrub_stale`
  acts on the apply's second and fresher topology read, and displacement
  detection keys on CRTC liveness rather than hotplug-detect state.
- A monitor is never left connected-but-dark. That state is now detected and
  forces a re-apply (with bounded retries) instead of being treated as a stable
  resting state the daemon would never revisit.
- A replug no longer moves windows twice. A disconnect arriving within
  `BOUNCE_SUSPECT_MS` of an apply is treated as a possible bounce and re-read for
  up to `BOUNCE_HOLDDOWN_MS` before being believed, suppressing the redundant
  off/on cycle. Disconnect edges only — connect edges and genuine unplugs are
  never delayed. Set `BOUNCE_HOLDDOWN_MS=0` to disable.
- The focused window is preserved across an unplug/replug: the steady-state
  selection is captured before the window manager can react and given back once
  per relocation cycle, rather than leaving focus wherever the last restore step
  landed.
- dwm command rejections are surfaced as `relocate_ipc_rejected` at WARNING
  instead of being silently treated as success, and `tagmon` never emits a
  negative direction — some dwm-ipc builds reject a negative argument outright.
  This was latent on 3-or-more-monitor setups only.

### Notes
- Fullscreen state is captured but not reapplied on restore; cross-monitor moves
  need ≥2 heads; `tagmon` is relative. Documented in the README.
- The expanded ruff ruleset now covers **all** of `src/xrandrw/`, not just the new
  window-management modules; the earlier scoping deferral is closed.
- The unplug/replug fixes above were confirmed by a physical unplug/replug on a
  single machine (a Dell laptop, `eDP-1` + `HDMI-1`, dwm 6.5 with dwm-ipc), in
  addition to the headless suite. Other hardware and multi-head configurations
  are covered by the automated tests only.

## [0.1.1] - 2026-07-18

Correctness pass over placement persistence, internal-panel detection, and the
event loop, plus touchscreen remapping and the first real CI.

### Added
- `TOUCH_MAP` config key — re-maps touch/stylus devices to their output after
  *every* apply. `xinput map-to-output` bakes in the output's geometry at call
  time, so a one-shot login-time remap goes stale whenever a panel moves
  (plug/unplug/side-swap); re-applying per-apply is the fix. Format
  `"<device-substring>:<OUTPUT>[;...]"`, case-insensitive, multiple devices
  supported. Empty by default, so `xinput` stays an optional dependency. Never
  maps onto a disconnected output.
- CI (`ci.yml`): `pytest` across Python 3.9 / 3.11 / 3.13 and a `vulture`
  dead-code job, both in virtualenvs.
- Release automation (`release.yml`): building on a published GitHub Release,
  attaching artifacts, and uploading to PyPI via `PYPI_API_TOKEN`.
- PyPI version/pyversions/license badges in the README.

### Changed
- **State file location.** `state_path()` now honours `XDG_DATA_HOME` and is
  resolved at call time rather than frozen into a module constant. If you have
  `XDG_DATA_HOME` set to something other than `~/.local/share`, your `state.json`
  moves accordingly — the remembered EDID-to-profile map and attach-order stack
  live at `$XDG_DATA_HOME/xrandrw/state.json`. Unset, the path is unchanged.
- The systemd helper unit no longer declares a backwards
  `Wants=dwm-session.target`; a session helper should only `WantedBy` + `PartOf`
  its session target.

### Fixed
- **`--set-pref` had no effect on placement.** `apply_once` placed externals by
  attach-stack *index* and never read `preferred_side`; its only reader was dead
  code. A monitor explicitly set `left-of` still landed `right-of`, defeating the
  core "put monitors where you left them" promise. Placement now takes
  `(item, preferred_side)` pairs — each display lands on its stored side,
  collisions fall back to the next free side, and chains of 5+ externals still
  resolve.
- **The internal panel is now always primary on DSI/DPI hardware.** Internal-LCD
  detection matched only `eDP`/`LVDS`, so on a Raspberry Pi (DSI) the built-in
  panel was not forced primary and won only by alphabetical luck in the
  no-internal fallback path. `DSI` and `DPI` now qualify.
- **No more redundant second apply on every hotplug.** `apply_once`'s own
  `xrandr` calls emit RandR notifications; the watch loop returned the *pre*-apply
  topology hash, so the settled post-apply state read as a fresh change and
  triggered an idempotent-but-wasteful second apply. It now returns the
  post-apply hash and absorbs its own events.
- Tests can no longer read or write the real `~/.local/share/xrandrw/state.json`
  — an autouse fixture redirects `XDG_DATA_HOME` per test, closing a hole where
  unisolated `apply_once` tests wrote junk outputs into live user state.

## [0.1.0] - 2026-07-05

Initial packaged baseline. This tags the still-hardcoded, as-yet-untested code
as an honest v0.1.0 starting point so the versioning scheme is established
immediately; later releases bump toward v1.0 as hardening, native X11, device
profiles, and tests land.

### Added
- PEP 621 `pyproject.toml` (setuptools backend, src-layout) declaring the
  `xrandrw = xrandrw.cli:main` console-script.
- MIT `LICENSE`.
- `README.md` documenting install, the six CLI modes, config keys, and the
  systemd user-service setup.
- `journald` optional-dependency extra for `systemd-python`.

### Changed
- `xrandrw.py` monolith split into the `src/xrandrw/` package (8 submodules).
- Makefile `install` target now runs `pipx install --force .` instead of copying
  a binary into `~/.local/bin`.
