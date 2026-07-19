#!/usr/bin/env bash
# TEST-06 mutation-runner SANITY-KILL (B1 false-0-kill guard).
#
# TOOL SELECTED: mutmut 3.6  (cosmic-ray.toml is the drafted fallback -- used ONLY if this
#                             sanity-kill cannot go green in the time-box).
#
# Purpose: PROVE the mutation runner can actually KILL a mutant in THIS repo BEFORE any
# baseline/ratchet gate is trusted. RESEARCH-mutation Pitfall 1 (repro'd here as
# "173/173 survived, 0 killed"): an EDITABLE install (`pip install -e`) drops a `.pth` that,
# together with pytest's `pythonpath=["src"]`, shadows mutmut's mutated `mutants/src` copy with
# the UN-mutated source -- so every mutant "survives" and `mutmut run` still EXITS 0. A gate
# ratcheted onto that is worse than no gate.
#
# This script defeats the trap by:
#   1. building a THROWAWAY, NON-editable venv (NO `pip install -e`; `pip uninstall -y xrandrw`),
#   2. relying on `[tool.mutmut] pytest_add_cli_args = ["-o","pythonpath="]` to clear the pytest
#      src override so the mutated copy is the one imported,
#   3. running ONLY the `parse_header` mutants (fast) -- the concrete sanity target is the
#      dwmipc.parse_header size-cap `>`->`>=` boundary, covered by tests/test_dwmipc_parse.py
#      and tests/test_mutation_kills.py,
#   4. asserting the runner reported at least one `: killed` in `mutmut results` (NOT trusting
#      the exit code). Zero kills => hard FAIL with a message to switch to cosmic-ray.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$(mktemp -d)/sanity-venv"
cleanup() { rm -rf "$(dirname "$VENV_DIR")" "$REPO_ROOT/mutants"; }
trap cleanup EXIT

echo "[sanity-kill] building NON-editable venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "python-xlib>=0.31" pytest "mutmut==3.6.*"
# CRITICAL (B1): the package must NOT be importable from an editable .pth, or the mutated
# mutants/src copy is shadowed by un-mutated source and every mutant falsely "survives".
"$VENV_DIR/bin/pip" uninstall -y xrandrw >/dev/null 2>&1 || true

# Fresh run dir so a stale mutants/ can't mask a real result.
rm -rf "$REPO_ROOT/mutants"

echo "[sanity-kill] running mutmut over parse_header mutants (scoped, non-editable venv)"
# mutmut exits 0 even when everything survives -- never trust its exit code.
"$VENV_DIR/bin/mutmut" run "*parse_header*" || true

# NOTE (mutmut 3.6 gotcha): `mutmut results` lists ONLY survivors/suspicious/timeouts --
# it does NOT print a `: killed` line for killed mutants -- so `mutmut results | grep ': killed$'`
# can NEVER match even when dozens are killed. The authoritative kill count is the
# `killed` field of mutants/mutmut-cicd-stats.json written by `export-cicd-stats`.
"$VENV_DIR/bin/mutmut" export-cicd-stats >/dev/null 2>&1 || true
KILLED="$("$VENV_DIR/bin/python" - <<'PY'
import json
try:
    d = json.load(open("mutants/mutmut-cicd-stats.json"))
    print(int(d.get("killed", 0)))
except Exception:
    print(0)
PY
)"
echo "[sanity-kill] mutmut reported killed=$KILLED (from mutmut-cicd-stats.json)"

if [ "${KILLED:-0}" -ge 1 ]; then
    echo "[sanity-kill] PASS: the runner killed >=1 mutant -- mutation gate is trustworthy."
    exit 0
fi

echo "[sanity-kill] FAIL: 0 mutants killed. The false-0-kill trap is NOT defeated in this env."
echo "[sanity-kill] ACTION: switch the mutation tool to cosmic-ray (see cosmic-ray.toml):"
echo "              cosmic-ray init cosmic-ray.toml session.sqlite && cosmic-ray exec cosmic-ray.toml session.sqlite"
echo "              cr-rate session.sqlite --fail-over 25"
exit 1
