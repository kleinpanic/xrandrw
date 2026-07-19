# `tests/functional/` — real-dwm/X functional harness (TEST-05)

This suite replaces the "everything is mocked" gap flagged in the pre-release
senior-dev review with a gate that drives the **real** `xrandrw.dwmipc`
control+capture path against a **real** patched dwm on a **real** private
dwm-ipc socket — no mocked dwm, no mocked Xlib.

Run it with the `functional` marker:

```bash
pytest -m functional tests/functional
```

## The 3-layer verification model

xrandrw's window-relocation feature has one hard, verified constraint: relocation
keys on `Output.connected == (oi.connection == randr.Connected)` (`xrandr.py:53`),
and neither `Xvfb` nor `xf86-video-dummy` outputs ever toggle `connected`. So a
**true** headless unplug/replug cannot be fired on a plain CI runner. We therefore
verify in three honestly-scoped layers:

| Layer | Where | What it proves | Gating? |
|-------|-------|----------------|---------|
| **L1** | this suite, plain **Xvfb** + `xrandr --setmonitor` + real patched dwm | the full **control + capture** path against reality: focus-then-act, real cross-monitor `tagmon`, `tag`/`togglefloating`/`configure`, real `get_dwm_client` capture, dwm crash-safety, and the coordinator record→restore pipeline driven by **injecting** the removed/returned output sets | **YES** — `functional` CI job |
| **L2** | `functional-vkms` CI job, real **Xorg** + `xf86-video-dummy` (+ best-effort VKMS) | a best-effort attempt at a **true** output flip where it can matter | NO — `continue-on-error` |
| **L3** | `14-05` live 2-monitor **HDMI** human-verify on real hardware | the ONLY place the true connect/disconnect → RandR-event → relocate → restore chain runs end-to-end | gates the v0.2.0 release |

## Honesty contract — what L1 does and does NOT prove

**L1 proves control + capture against real dwm.** It stands up a plain `Xvfb`
(no root, no `Xorg`, no dummy driver, no tty), uses `xrandr --setmonitor` (RandR
1.5 → Xinerama) so the real dwm sees **≥ 2 monitors**, builds and runs a real
dwm-ipc-patched dwm on a **private** socket, spawns real `xterm` windows, and
asserts every `dwmipc` verb and capture read against what dwm actually reports.

**L1 does NOT prove a true output-status RandR flip through `watch_loop`.** Under
Xvfb — exactly as under `xf86-video-dummy` — outputs never toggle `connected`, so
`--off`/`--auto`/`--setmonitor` never produce the daemon's removed/returned output
sets (verified against `xrandr.py:53`, `relocate.py:293`). L1 therefore proves the
record→restore path by **injection**: it calls
`RelocationCoordinator._record_displaced` / `_restore_returned` (and `on_settled`
with a stub reader whose outputs carry a flipped `connected`) directly against the
real socket. **The true unplug→replug chain is proven ONLY by the live L3 HDMI
verify (plan 14-05); the L2 real-Xorg+dummy+VKMS job is best-effort/non-gating.**

## Socket safety (T-14-01)

The harness dwm is launched as `dwm -s "$DWM_SOCKET"` with `$DWM_SOCKET` pointing
at a **private** path under `$RUNNER_TEMP` / a short temp dir — **never** the
world-writable `/tmp/dwm.sock`. The Python client reads the same path from
`$DWM_SOCKET` (`dwmipc.DEFAULT_SOCK_PATH`, `dwmipc.py:57`); `conftest.py` asserts
server and client agree and that the path is not `/tmp/dwm.sock`. The suite is
fully isolated from any developer's live `DISPLAY=:0` / running dwm — it picks a
free display ≥ `:99` and its own dwm instance.

## Skip vs. fail (M2 — no vacuous green)

* **On CI** (`$GITHUB_ACTIONS` set): a missing `Xvfb`/dwm build dep or any X/dwm
  launch failure is an **ERROR/FAILURE, never a skip**, and `test_functional_floor`
  asserts that **> 0** functional tests were collected — so a silently-empty gating
  job fails instead of reporting green.
* **Locally**: the same conditions `skip` cleanly so unit dev/CI stays fast. On a
  box that has `Xvfb` + the dwm build toolchain, the suite **runs green** (it builds
  its own throwaway dwm and needs no pre-existing X server).

## Vendored dwm (`dwm/`)

* `dwm/dwm-ipc.diff` — dwm 6.5 + mihirlad55/dwm-ipc (pinned; see the diff header),
  rebased to apply cleanly to 6.5 with **no fuzz**, plus a `-s <socketpath>` flag.
  Links `libyajl` at build (CI installs `libyajl-dev` + `yajl`).
* `dwm/config.h` — the dwm-ipc `config.h` with the `ipccommands[]` table the
  patch requires.
* `dummy-2head.conf` — the L2-only `xf86-video-dummy` 2-head Xorg config.
