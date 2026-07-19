from __future__ import annotations
import logging
import os
import select
import signal
import threading
import time

from Xlib import display
from Xlib.ext import randr

from xrandrw.logging_utils import logev
from xrandrw.xrandr import RandRReader, topology_hash, unhealed_outputs
from xrandrw.apply import apply_once, _sd_notify

stop_evt = threading.Event()

# How many consecutive re-applies we will spend trying to light a connected head
# whose CRTC is dark, before accepting the state and absorbing the hash. Without a
# bound, hardware that simply refuses to light (a dead cable, an unsupported mode)
# would re-apply on every wakeup forever.
UNHEALED_APPLY_LIMIT = 3


def unhealed_connectors(logger: logging.Logger | None = None) -> list[str]:
    """Live read of :func:`unhealed_outputs` -- the module seam tests patch.

    Costs ONE extra RandR read, and only on the post-apply path (applies are rare
    -- they need a topology change), so the steady-state slow-poll wakeup is
    unaffected. A failed read returns [] rather than raising or guessing: an
    UNKNOWN topology is not a known-bad one, and forcing re-applies off a read we
    could not complete is how you build a spin loop.
    """
    try:
        return unhealed_outputs(RandRReader().read(logger))
    except Exception as e:
        logev(logger, logging.DEBUG, "watch_unhealed_read_fail",
              "could not read topology for the unhealed-state check", error=str(e))
        return []


def present_connectors(logger: logging.Logger | None = None) -> set[str]:
    """Names of currently CONNECTED connectors -- the module seam tests patch.

    Same shape and same swallow rationale as :func:`unhealed_connectors`: an
    unreadable topology yields the empty set, and every caller treats "unknown"
    as "do not hold down" (see :func:`_bounce_settled`). Never guess an edge
    direction from a read that did not complete.
    """
    try:
        return {n for n, o in RandRReader().read(logger).items() if o.connected}
    except Exception as e:
        logev(logger, logging.DEBUG, "watch_present_read_fail",
              "could not read topology for the bounce-edge check", error=str(e))
        return set()


def _bounce_settled(env: dict[str, str], logger: logging.Logger,
                    last_hash: str, churn: dict) -> bool:
    """True IFF a DISCONNECT edge resolved back to `last_hash` within the hold-down.

    UX-02. A physical replug does not present one clean connect edge -- the connector
    drops again shortly after coming back. The daemon believed that second drop, and
    `scrub_stale` powered the head off (`--off`), so dwm evacuated the windows that had
    just been restored and the unhealed self-heal then dragged them back. That is FOUR
    visible window movements for ONE replug, two of them pure artefact.

    Suppressing it is a WAIT, not a smarter predicate: the only way to tell a bounce from
    an unplug is to look again later. The cost of looking later is latency on a GENUINE
    unplug -- the laptop panel sits in the oversized two-head geometry with windows
    off-screen until the apply runs. So the wait is GATED on two conditions, and both
    gates are measured, not asserted (evidence/newdaemon2.log, ms resolution;
    evidence/livetest-PASS-2026-07-19-1929.log, 1 s resolution):

    1. RECENCY. Time from the previous apply_done to the disconnect:
           bounce  04:21:42,917 -> 43,127  =    0.21 s
           bounce  19:29:08     -> 19:29:09 =  ~1 s      (1 s log resolution)
           genuine 04:20:48,146 -> 04:21:35,554 =  47.4 s
           genuine 04:21:47,077 -> 04:23:57,780 = 130.7 s
           genuine 19:24:54     -> 19:29:02     =  248 s
       Two clusters ~2 orders of magnitude apart. BOUNCE_SUSPECT_MS=5000 sits ~5x above
       the largest bounce and ~9x below the smallest genuine unplug. A genuine unplug
       therefore pays ZERO added latency -- which is the whole point, because genuine
       unplugs are the common case and bounces the rare one.

    2. EDGE DIRECTION. Only a DISCONNECT is held. The connect edge stays instant, and
       that is not a nicety: the genuine replug at 04:21 landed 3.1 s after apply_done
       (42,917 -> 40,564 is the prior pair; 37,419 -> 40,564 = 3.1 s), i.e. INSIDE the
       5 s suspect window. Gating on recency alone would delay real replugs.

    BOUNCE_HOLDDOWN_MS is a CONSERVATIVE BOUND, NOT A MEASURED BOUNCE DURATION, and no
    future comment may upgrade that claim. The dark interval could not be resolved from
    either trace, because in both the daemon was blocked inside its own `--off` modeset
    (measured 1.36-1.50 s) for most of it:
        04:21  disconnect 43,127 -> connected again by 44,845  =>  <= 1.72 s
        19:29  disconnect ~09.0  -> connected again by ~11.x   =>  <= ~3.0 s (1 s resolution)
    3000 ms clears the 04:21 bound with ~1.7x margin but only MEETS the 19:29 bound. It is
    defensible only because gate 1 makes over-waiting nearly free; it is not defensible as
    a measurement. If a live replug still shows the double cycle, raise it -- that is why
    it is configurable.

    Worst case if the head really is gone: we waited BOUNCE_HOLDDOWN_MS and then behave
    exactly as before. No new failure mode, no new state.
    """
    hold_ms = int(env.get("BOUNCE_HOLDDOWN_MS", 0) or 0)
    if hold_ms <= 0:
        return False  # documented kill-switch
    suspect_ms = int(env.get("BOUNCE_SUSPECT_MS", 0) or 0)
    done_at = churn.get("apply_done_at")
    if done_at is None or (time.monotonic() - done_at) * 1000.0 > suspect_ms:
        return False  # gate 1: not recent enough to be a bounce -> genuine, heal NOW
    prev = churn.get("present")
    if not prev:
        return False  # no trusted baseline (or an unreadable read) -> never hold down
    if not (prev - present_connectors(logger)):
        return False  # gate 2: nothing vanished -> connect edge -> instant
    logev(logger, logging.DEBUG, "watch_bounce_hold",
          "disconnect within the bounce-suspect window; re-reading before applying",
          hold_ms=hold_ms, suspect_ms=suspect_ms)
    deadline = time.monotonic() + hold_ms / 1000.0
    while time.monotonic() < deadline:
        # WR-01: stop_evt.wait(), never time.sleep(). A multi-second hold must not
        # add multi-second SIGTERM latency. NOTE this runs BEFORE apply_once, so no
        # apply-lock is held across the wait -- a concurrent apply is never blocked.
        if stop_evt.wait(min(0.15, max(0.0, deadline - time.monotonic()))):
            return False  # shutting down; let the loop head exit, do not suppress
        if topology_hash(logger) == last_hash:
            logev(logger, logging.INFO, "watch_bounce_absorbed",
                  "connector bounced and returned; suppressed a redundant off/on cycle",
                  hold_ms=hold_ms)
            return True
    return False

def _install_signals(logger: logging.Logger):
    def _sig(sig, frame):
        logev(logger, logging.INFO, "shutdown", "signal received", sig=sig)
        stop_evt.set()
        _sd_notify("STOPPING=1")
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

def _apply_if_changed(env: dict[str, str], logger: logging.Logger,
                      last_hash: str, churn: dict, got_event: bool, coordinator=None) -> str:
    # Decision logic isolated from the blocking select() so it is unit-testable
    # with topology_hash/apply_once mocked and no live Display.
    cur = topology_hash(logger)
    if cur == last_hash:
        return last_hash
    now = time.monotonic()
    churn["times"].append(now)
    churn["times"] = [t for t in churn["times"] if now - t <= churn["window"]]
    if len(churn["times"]) > churn["threshold"]:
        logev(logger, logging.WARNING, "watch_excess", "excess topology churn",
              count=len(churn["times"]), window=churn["window"])
        churn["backoff"] = min(1000, churn["backoff"] + 150)
    else:
        churn["backoff"] = max(0, churn["backoff"] - 50)
    logev(logger, logging.DEBUG, "watch_change", "topology hash changed",
          debounce_ms=150 + churn["backoff"])
    # Debounce a burst: one physical plug emits Crtc+Output+ScreenChange (Pitfall 6).
    time.sleep((150 + churn["backoff"]) / 1000.0)
    verify = topology_hash(logger)
    if verify == last_hash:
        return last_hash
    # UX-02: the 150 ms debounce above absorbs ONE plug's event burst; it is far too
    # short for the physical re-drop that follows a replug. _bounce_settled applies a
    # second, LONGER, and deliberately ASYMMETRIC wait -- disconnect edges only, and
    # only when suspiciously soon after an apply. Returning last_hash here means
    # apply_once never runs, so scrub_stale never issues the --off, so dwm never
    # evacuates: the whole avoidable movement pair disappears at the source.
    if _bounce_settled(env, logger, last_hash, churn):
        return last_hash
    src = "randr_event" if got_event else "slow_poll"
    logev(logger, logging.INFO, "watch_apply", "apply on topology change", source=src)
    applied = apply_once(env, logger, event_source=src)
    if not applied:
        # BL-01: apply_once BAILED (lock refused / another apply running / either xrandr
        # read failed). The topology is UNKNOWN and certainly unhealed -- in particular a
        # read-#2 bail returns above scrub_stale, so a disconnected-but-lit head is still
        # powered on. Absorbing the new hash here would freeze change detection on that
        # state forever (no further apply until another physical event = a phantom dwm
        # monitor that never goes away). Returning the OLD hash means the next wakeup
        # still sees a difference and retries.
        #
        # We also do NOT run the settle hook (WR-01). With CRTC-liveness `cur`, a
        # transiently dark read is a `removed` edge; on a bail that edge is an artefact of
        # an observation we could not confirm. A spurious `removed` records windows dwm
        # never evacuated, and the next settle sees `returned` and runs _restore_returned
        # -- whose plan_restore UNCONDITIONALLY re-emits the saved tag bitmask, silently
        # resetting a tag the user changed since the snapshot. Never mutate window state
        # off an unconfirmed observation.
        logev(logger, logging.INFO, "watch_apply_incomplete",
              "apply did not complete; not absorbing topology hash", source=src)
        return last_hash
    # Absorb our own mutations: the apply's xrandr commands emit RandR events; re-read the
    # settled topology so the loop doesn't chase its own change into a redundant 2nd apply.
    settled = topology_hash(logger)
    # UX-02 bounce baseline. Recorded on EVERY post-apply path (including the unhealed
    # early-return below), because a bounce can follow any completed apply. Costs one
    # RandR read -- measured 9 ms on evidence/newdaemon2.log (04:21:40,728->40,737, the
    # read#1->read#2 gap when no modeset was due) against an apply that spends 1.4 s in
    # a single HDMI modeset, so it is noise. Skipped entirely when the hold-down is off.
    if int(env.get("BOUNCE_HOLDDOWN_MS", 0) or 0) > 0:
        churn["apply_done_at"] = time.monotonic()
        churn["present"] = present_connectors(logger)
    # Is the settled topology actually HEALTHY? A connected head with no CRTC is a
    # known-bad RESTING state (see xrandr.unhealed_outputs) and its hash is stable,
    # so absorbing `settled` here would short-circuit the loop forever and leave the
    # monitor BLACK. Read it BEFORE the coordinator hook so the hook cannot perturb
    # what we observed.
    unhealed = unhealed_connectors(logger)
    # Phase-10 additive hook (WM-06/WM-08): the relocation coordinator runs ONLY on this
    # post-apply branch, AFTER the settled hash is frozen. It must NOT alter the returned
    # hash or add a second topology read (preserves the no-double-apply invariant), and a
    # coordinator fault must never break the watch loop -- guarded + swallowed. Default
    # coordinator=None keeps the loop byte-for-byte identical for existing callers/tests.
    if coordinator is not None:
        try:
            # WR-01: thread the shutdown flag so a SIGTERM during the synchronous
            # restore cycle bails promptly instead of masking behind a slow dwm.
            coordinator.on_settled(env, logger, stop_evt=stop_evt)
        except Exception as e:
            logev(logger, logging.WARNING, "relocate_hook_fail",
                  "relocation hook raised; ignoring (display layout unaffected)", error=str(e))
    if unhealed:
        # NOTE the hook ran ABOVE this, deliberately. Unlike the `not applied` bail,
        # here the apply COMPLETED and the topology is fully known -- known-BAD, but
        # known. The displacement is real (the head went dark, dwm evacuated), so the
        # `removed` edge MUST be recorded now; if we skipped the hook, the healing
        # re-apply below would re-light the head and the following settle would see
        # neither `removed` nor `returned`, and the windows would never come back.
        # "Unconfirmed observation" (WR-01) and "confirmed bad state" are different
        # things and get different treatment.
        attempts = churn.get("unhealed", 0) + 1
        churn["unhealed"] = attempts
        if attempts <= UNHEALED_APPLY_LIMIT:
            logev(logger, logging.WARNING, "watch_unhealed",
                  "connected output has no CRTC after apply; forcing a re-apply",
                  outputs=unhealed, attempt=attempts, limit=UNHEALED_APPLY_LIMIT)
            # Do NOT absorb: returning the OLD hash keeps `cur != last_hash` on the
            # next wakeup, so the loop re-applies and lights the head.
            return last_hash
        logev(logger, logging.WARNING, "watch_unhealed_giveup",
              "output still has no CRTC after repeated applies; accepting state to "
              "avoid a re-apply loop (monitor may stay dark until the next event)",
              outputs=unhealed, attempts=attempts)
    churn["unhealed"] = 0
    return settled

def watch_loop(env: dict[str, str], logger: logging.Logger, coordinator=None):
    slow_poll = int(env["POLL_INTERVAL"])  # D-06: safety-net timeout, not a tight loop
    churn = {
        "times": [],
        "backoff": 0,
        "window": int(env["EXCESS_WINDOW_SEC"]),
        "threshold": int(env["EXCESS_THRESHOLD"]),
    }
    try:
        d = display.Display()
    except Exception as e:
        # Graceful-degrade: a failed connect logs and returns (systemd Restart re-runs us).
        logev(logger, logging.ERROR, "xlib_connect_fail", "cannot open X display for watch", error=str(e))
        return
    root = d.screen().root
    ver = d.xrandr_query_version()
    # Pitfall 5: randr.init only wires RRNotify subevents on server RandR >= 1.5.
    events_ok = (ver.major_version, ver.minor_version) >= (1, 5)
    if events_ok:
        mask = (randr.RRScreenChangeNotifyMask
                | randr.RROutputChangeNotifyMask
                | randr.RRCrtcChangeNotifyMask)
        root.xrandr_select_input(mask)
        d.flush()
    else:
        logev(logger, logging.WARNING, "watch_degrade",
              "RandR < 1.5: event registration unavailable, slow-poll only",
              version=f"{ver.major_version}.{ver.minor_version}")
    xfd = d.fileno()
    # D-05/Pitfall 3: signal.set_wakeup_fd writes signo to wpipe on delivery, waking
    # select() instantly so SIGTERM does not wait out the slow-poll timeout.
    rpipe, wpipe = os.pipe()
    os.set_blocking(rpipe, False)
    os.set_blocking(wpipe, False)
    old_wakeup = signal.set_wakeup_fd(wpipe)
    last = topology_hash(logger)
    logev(logger, logging.INFO, "watch_start", "watch: event-driven",
          slow_poll=f"{slow_poll}s", events=events_ok)
    try:
        while not stop_evt.is_set():
            r, _, _ = select.select([xfd, rpipe], [], [], slow_poll)
            if rpipe in r:
                try:  # noqa: SIM105 - deliberate best-effort drain swallow (CLAUDE.md error-handling convention)
                    os.read(rpipe, 64)  # drain; _install_signals already set stop_evt
                except BlockingIOError:
                    pass
                continue  # loop head re-checks stop_evt -> prompt clean exit
            got_event = False
            if events_ok:
                while d.pending_events():
                    d.next_event()
                    got_event = True
            if got_event or (xfd not in r):  # event burst OR slow-poll timeout fired
                last = _apply_if_changed(env, logger, last, churn, got_event, coordinator)
    finally:
        signal.set_wakeup_fd(old_wakeup)
        os.close(rpipe)
        os.close(wpipe)
        d.close()
