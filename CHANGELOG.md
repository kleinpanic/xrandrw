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
  diagnostic printing the live captured/displaced window records as JSON.
- CI: a `--cov-fail-under=90` coverage gate on the new modules, an expanded
  `ruff` ruleset (`B`/`SIM`/`PERF`/`C90`/`UP`/`RUF`) job scoped to them, and a
  headless functional suite (a fake `AF_UNIX` dwm-ipc server + mocked Xlib +
  fake `/proc`) that exercises the full lifecycle with no X server or hardware.
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
- Expanding the new ruff rulesets across the v0.1.0 modules is a tracked
  follow-up (the gate is scoped to the new window-management modules for now).
- The unplug/replug fixes above were confirmed by a physical unplug/replug on a
  single machine (a Dell laptop, `eDP-1` + `HDMI-1`, dwm 6.5 with dwm-ipc), in
  addition to the headless suite. Other hardware and multi-head configurations
  are covered by the automated tests only.

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
