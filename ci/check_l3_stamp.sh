#!/usr/bin/env bash
# TEST-05/CD-03 release gate: is the L3 live-hardware verify stamp actually FRESH?
#
# WHY THIS EXISTS AS A FRESHNESS CHECK AND NOT AN EXISTENCE CHECK
# ---------------------------------------------------------------
# The window-management relocate/restore path can only be proven by a REAL physical
# unplug/replug against a REAL dwm. No CI runner can flip a connector's hotplug-detect
# bit, and `xrandr --off` does not simulate it (HPD stays connected, so the daemon just
# re-lights the head inside one apply). So a human-run verification, recorded in a
# committed stamp, is the only evidence that path has ever executed on metal.
#
# The FIRST version of this gate ran `test -f tests/functional/L3_PASS.stamp` and nothing
# else. That is worse than having no gate, because it LOOKS like one: the stamp records a
# commit, but nothing ever compared it to what was being released. It would have passed
# forever, on every future commit, however far the code drifted from the verified state.
# That is precisely the failure mode this file exists to remove.
#
# THE DRIFT POLICY, AND WHY THIS ONE
# ----------------------------------
# Chosen policy:  FAIL if src/xrandrw/ changed since the verified commit.
#                 WARN (loudly, in the job summary) for any other drift.
#
# The stamp certifies RUNTIME BEHAVIOUR. Anything under src/xrandrw/ can invalidate that,
# so shipping such a change on old evidence is exactly the thing worth blocking. Changes to
# docs, tests, or CI config cannot alter the shipped runtime, so blocking on them would mean
# physically unplugging an HDMI cable to land a typo fix -- a cost high enough that the
# realistic outcome is someone deletes the gate. A gate people route around protects nothing.
# Hence: hard-fail exactly where the evidence can actually go stale, warn everywhere else.
#
# THE WAIVER, AND WHY IT IS NOT JUST THE OLD RUBBER STAMP
# ------------------------------------------------------
# `drift_waived_through:` lets a human attest that specific, reviewed src/ changes do not
# invalidate the live evidence. It differs from the old always-passes gate in three ways
# that matter:
#   1. It is PINNED TO A SHA. It cannot pass "forever on any future commit" -- the moment
#      one more commit touches src/xrandrw/, the residual diff is non-empty and this fails.
#   2. It requires editing a committed file with a written reason, so it appears in review.
#   3. The gate prints exactly which files it is waiving, into the job summary.
# It is a deliberate, auditable, expiring act. Re-running the live verify remains the
# correct path; the waiver is the honest escape hatch for provably-unrelated drift.
#
# Usage:  ci/check_l3_stamp.sh [released-ref]     (default: HEAD)
#         Set TAG_VERSION to override the version parsed from GITHUB_REF_NAME.
#
# NOTE: requires full history. `actions/checkout` MUST use `fetch-depth: 0`, otherwise the
# ancestry and diff checks below operate on a shallow clone and cannot see the stamped commit.
set -euo pipefail

STAMP="${STAMP_PATH:-tests/functional/L3_PASS.stamp}"
REF="${1:-HEAD}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

fail() { echo "::error::$*" >&2; echo "- **FAIL:** $*" >> "$SUMMARY"; exit 1; }
warn() { echo "::warning::$*" >&2;  echo "- **WARN:** $*" >> "$SUMMARY"; }
note() { echo "$*";                 echo "- $*"          >> "$SUMMARY"; }

echo "## L3 live-hardware verify stamp" >> "$SUMMARY"

[ -f "$STAMP" ] || fail "$STAMP is missing -- the live 2-monitor hardware verify has not been recorded for this release."

# ---- parse the machine-readable fields -------------------------------------------------
# ANCHORED AT COLUMN 0 ON PURPOSE. An earlier version allowed leading whitespace and
# matched an INDENTED PROSE MENTION of "drift_waived_through:" in this stamp's own policy
# paragraph, parsing the sentence that followed as a commit sha. Real fields start at
# column 0; any indented occurrence is prose and must not be read as data.
field() {
  awk -v k="$1" 'index($0, k ":") == 1 { sub("^" k ":[ \t]*", ""); print $1; exit }' "$STAMP"
}

VERIFIED="$(field verified_at_commit)"
CERTIFIES="$(field certifies_version)"
WAIVED="$(field drift_waived_through)"

[ -n "$VERIFIED" ]  || fail "stamp has no 'verified_at_commit:' field -- cannot tell what was verified."
[ -n "$CERTIFIES" ] || fail "stamp has no 'certifies_version:' field -- cannot tell which release it certifies."

git rev-parse --verify --quiet "${VERIFIED}^{commit}" >/dev/null \
  || fail "stamp's verified_at_commit '$VERIFIED' is not a commit in this repository (shallow clone? needs fetch-depth: 0)."

REF_SHA="$(git rev-parse --short "$REF")"

# ---- 1. the stamp must certify THIS release, so each release needs its own -------------
TAG="${TAG_VERSION:-${GITHUB_REF_NAME:-}}"
TAG="${TAG#v}"
if [ -n "$TAG" ]; then
  [ "$CERTIFIES" = "$TAG" ] \
    || fail "stamp certifies version '$CERTIFIES' but this release is '$TAG'. Re-run the live verify and update the stamp; a stamp from a previous release does not carry forward."
  note "Stamp certifies version \`$CERTIFIES\`, matching the release."
else
  warn "no release tag in the environment; skipping the certifies_version match (local run?)."
fi

# ---- 2. the verified commit must actually be in this release's history -----------------
git merge-base --is-ancestor "$VERIFIED" "$REF" \
  || fail "stamp's verified_at_commit '$VERIFIED' is NOT an ancestor of the released ref '$REF_SHA'. The stamp describes a commit this release is not built from."
note "Verified commit \`$VERIFIED\` is an ancestor of released ref \`$REF_SHA\`."

# ---- 3. drift ---------------------------------------------------------------------------
SRC_DRIFT="$(git diff --name-only "$VERIFIED" "$REF" -- src/xrandrw/ || true)"
ALL_DRIFT="$(git diff --name-only "$VERIFIED" "$REF" || true)"

if [ -z "$ALL_DRIFT" ]; then
  note "**PASS** -- released ref is identical to the verified commit."
  exit 0
fi

if [ -z "$SRC_DRIFT" ]; then
  warn "the release has drifted $(echo "$ALL_DRIFT" | wc -l | tr -d ' ') file(s) from the verified commit, but NONE under src/xrandrw/. Runtime behaviour is unchanged, so the live evidence still applies. Proceeding."
  exit 0
fi

# src/ changed. Is it covered by an explicit, sha-pinned waiver?
RESIDUAL="$SRC_DRIFT"
if [ -n "$WAIVED" ]; then
  if ! git rev-parse --verify --quiet "${WAIVED}^{commit}" >/dev/null; then
    fail "stamp's drift_waived_through '$WAIVED' is not a commit in this repository."
  elif ! git merge-base --is-ancestor "$WAIVED" "$REF"; then
    fail "stamp's drift_waived_through '$WAIVED' is not an ancestor of the released ref '$REF_SHA' -- the waiver does not apply to this release."
  else
    RESIDUAL="$(git diff --name-only "$WAIVED" "$REF" -- src/xrandrw/ || true)"
    warn "src/xrandrw/ drifted since the verified commit, but a human waiver covers it through \`$WAIVED\`. Waived files:"$'\n'"$(git diff --name-only "$VERIFIED" "$WAIVED" -- src/xrandrw/ | sed 's/^/    - /')"
  fi
fi

if [ -n "$RESIDUAL" ]; then
  echo "::group::unverified src/xrandrw/ changes" >&2
  echo "$RESIDUAL" >&2
  echo "::endgroup::" >&2
  {
    echo "- **FAIL:** src/xrandrw/ changed after the live verify, with no waiver covering it:"
    echo "$RESIDUAL" | sed 's/^/    - /'
  } >> "$SUMMARY"
  echo "::error::The live-hardware evidence does not cover the code being released." >&2
  echo "::error::Either re-run the L3 live verify and update $STAMP, or add a reviewed" >&2
  echo "::error::'drift_waived_through:' sha with a written reason if the changes provably" >&2
  echo "::error::cannot affect the relocate/restore path." >&2
  exit 1
fi

note "**PASS** -- residual src/xrandrw/ drift is fully covered by the recorded waiver."
