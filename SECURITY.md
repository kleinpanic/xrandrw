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
