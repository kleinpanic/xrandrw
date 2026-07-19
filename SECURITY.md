# Security Policy

## Reporting

Report suspected vulnerabilities privately to the maintainer at
<kleinpanic@gmail.com>. Please do not open a public issue for undisclosed
security problems.

## Threat model / posture

`xrandrw` is a **local, single-user desktop** daemon. It runs as the logged-in
user and talks to the local X server and (optionally) the local `dwm` window
manager. It is not a network service and grants no privileges. The trust
boundary that receives untrusted bytes is the dwm-ipc endpoint
(`/tmp/dwm.sock`), which is hardened in `src/xrandrw/dwmipc.py` (SEC-01): every
reply is bounded before allocation (`MAX_REPLY_SIZE`), every socket op is
timed with a single total wall-clock deadline (no unbounded block, no
slow-trickle hold), every parse/transport error is funnelled to the single
`DwmIpcUnavailable` type (feature silently degrades OFF), and the socket path
is pre-checked to be a current-uid-owned socket before connecting.

## Accepted risks

The following are **deliberate design choices** appropriate to the local
single-user posture above, not defects:

1. **`subscribe()` returns an un-lifecycle-managed socket by design.**
   `dwmipc.subscribe()` performs the SUBSCRIBE handshake and returns the live,
   still-open socket to its caller. It does **not** read, loop, or close it. The
   EVENT-stream consumption loop and socket close/cleanup are owned by the
   Phase 10 relocation lifecycle caller. Until that caller exists, holding the
   returned socket open is intentional; the caller is responsible for closing
   it.

2. **`run_command` / `subscribe` return values are not shape-validated by
   design.** Unlike `get_monitors` / `get_dwm_client` (which run strict shape
   validators), `run_command`'s decoded result and the `subscribe` handshake
   are returned as-decoded. They remain bounded by two hard limits at the
   untrusted boundary: the `MAX_REPLY_SIZE` cap (enforced before any allocation)
   and JSON-only decoding (a non-JSON body raises `DwmIpcUnavailable`). Callers
   that need a specific shape validate it themselves; adding a rigid schema here
   would couple the client to dwm command-result shapes that are Phase 10
   concerns.

3. **Residual socket-path trust after the owner/type check.** `dwmipc` verifies
   `/tmp/dwm.sock` is a socket owned by the current uid (via `os.lstat` +
   `S_ISSOCK`, rejecting symlinks) before connecting. This defeats a foreign
   user pre-placing a file or socket at the path. It does **not** defend against
   a same-uid process pre-binding the path (which could already act as the user
   directly) — that is out of scope for a single-user desktop where all the
   user's own processes are already inside the trust boundary. dwm creates the
   socket as the same user, so the normal case is unaffected.

## Phase 10 — hotplug relocation lifecycle (accepted risks)

The relocation lifecycle (`src/xrandrw/relocate.py`) is the only component that
*mutates* window state. It stays inside the same local single-user trust
boundary and reuses the hardened `dwmipc` client for every round-trip. The
following are deliberate, documented choices for this phase.

- **T-10-SC — Dependency posture (unchanged from Phase 4/8).** Phase 10 adds no
  new third-party runtime dependency. Window control uses the already-audited
  `python-xlib` seam (own `Display` per call, never raises past the seam) and
  the Phase-8 `dwmipc` client (bounded reply size, single total wall-clock
  deadline, uid/type-checked socket path, all errors funnelled to
  `DwmIpcUnavailable` → feature degrades OFF). No network surface is introduced;
  the trust boundary is still the local dwm-ipc endpoint.

- **T-10-04-D — Synchronous in-loop restore (W2 tradeoff, now bounded).** The
  restore cycle runs synchronously inside the single-threaded watch `select()`
  loop rather than on a worker thread (the locked no-thread decision, W2). Its
  cost is bounded by a modest per-call `ipc_timeout` (`_RELOCATE_IPC_TIMEOUT`,
  0.25s, threaded into *every* dwm-ipc call including `capture_windows`) and the
  `n_monitors`-bounded tagmon hop count, with the cycle duration logged
  (`relocate_cycle_done`). WR-01 further bounds worst-case shutdown latency: the
  cycle checks the watch-loop `stop_evt` at the head of the per-window loop and
  bails after the current window on SIGTERM, so a slow-but-connected dwm can no
  longer delay shutdown behind a full batch (nor mask it from the watchdog).

- **`_displaced` eviction policy (WR-02) in place.** Displaced-window records are
  swept on every steady-state settle: any record whose `(pid, starttime)` no
  longer resolves to a live process (dead process or reused PID, checked against
  `/proc` with no window control) is dropped (`relocate_displaced_evict`). This
  bounds the in-memory map so a permanently-removed output or an exited process
  cannot leak records forever in a long-lived daemon.

- **Fullscreen state is captured but not restored (IN-03, known limitation).**
  A displaced window's `is_fullscreen` flag is recorded but intentionally *not*
  reapplied on restore. dwm supports `_NET_WM_STATE_FULLSCREEN`, but a
  headless-testable restore of that state could not be validated against the
  test doubles this phase, so it is omitted rather than shipped unverified. A
  window that was fullscreen on the removed output returns to its saved
  monitor/tag/floating-state/geometry but not its fullscreen flag. This is a
  functional gap, not a security risk; tracked for a follow-up phase.
