#!/usr/bin/env bash
#
# Demo driver. Runs the pipeline segment by segment, pausing before each one
# so you can flip a flag in the LaunchDarkly console.
#
#   demo/demo.sh check          preflight: keys, LD reachability, warm cache
#   demo/demo.sh all            the whole demo, in order, with pauses
#   demo/demo.sh 0 | baseline   what LaunchDarkly is serving right now
#   demo/demo.sh 1 | kill       pipeline-kill-switch
#   demo/demo.sh 2 | validation validation-mode standard -> strict
#   demo/demo.sh 3 | verify     verification-enabled off -> on
#   demo/demo.sh 4 | ledger     what the run rows recorded
#   demo/demo.sh reset          put every flag back and confirm
#
# It types the commands so you do not have to. It cannot flip the flags --
# that is the point of the demo and it happens in the console, on screen.
#
# Runs land in demo/ledger/ rather than the committed ledger/, so a rehearsal
# does not append five runs to an append-only file you ship. Override with
# DEMO_LEDGER=ledger to use the real one.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LEDGER="${DEMO_LEDGER:-demo/ledger}"
ROSTER="${DEMO_ROSTER:-demo/roster.yaml}"
OPERATOR="${DEMO_OPERATOR:-jeremy}"
OUT="demo/.out"

# uv does not read .env locally; the devcontainer sets UV_ENV_FILE for it.
# On Claude Code on the web there is no .env and the keys are already exported.
if [[ -f .env && -z "${UV_ENV_FILE:-}" ]]; then
    export UV_ENV_FILE=.env
fi

mkdir -p "$LEDGER" "$OUT"

BOLD=$'\033[1m'; DIM=$'\033[2m'; YEL=$'\033[33m'; GRN=$'\033[32m'; RST=$'\033[0m'

banner() { printf '\n%s%s%s\n\n' "$BOLD" "$*" "$RST"; }
note()   { printf '%s%s%s\n' "$DIM" "$*" "$RST"; }

# The only thing the script cannot do for you.
flag() {
    printf '\n%s  LaunchDarkly:%s %s\n' "$YEL" "$RST" "$1"
    printf '%s  press enter once it is saved%s ' "$DIM" "$RST"
    read -r _
}

pause() {
    printf '\n%s  press enter%s ' "$DIM" "$RST"
    read -r _
}

plan() {   # plan <output-label>
    local label="$1"
    uv run bbq-shipment-agent run plan \
        --recipients "$ROSTER" \
        --ledger "$LEDGER" \
        --operator "$OPERATOR" \
        2>&1 | tee "$OUT/$label.txt"
}

# The one-line summary of a saved run, for a before/after comparison.
summarize() {
    local label="$1"
    grep -E '^  (packets|total cost)|^B2 corrected|^escalated:' "$OUT/$label.txt" 2>/dev/null \
        | cut -c1-100 || true
}

cmd_check() {
    banner "Preflight"
    local ok=0
    for key in LD_SDK_KEY SHIPPO_API_KEY ANTHROPIC_API_KEY; do
        if grep -qE "^$key=.+" .env 2>/dev/null || [[ -n "${!key:-}" ]]; then
            printf '  %s✓%s %s\n' "$GRN" "$RST" "$key"
        else
            printf '  ✗ %s is unset. Segments 2 and 3 will not run.\n' "$key"; ok=1
        fi
    done
    [[ -f .cache/shippo-quotes.json ]] \
        && printf '  %s✓%s warm quote cache (%s)\n' "$GRN" "$RST" \
             "$(du -h .cache/shippo-quotes.json | cut -f1)" \
        || printf '  ! no warm quote cache — the first run will be slow. Run this once beforehand.\n'

    banner "LaunchDarkly, live"
    uv run bbq-shipment-agent run init --ledger "$LEDGER" --operator "$OPERATOR"
    note "
If 'connection' says offline, the demo still runs but every flag serves its
code default and nothing you flip in the console will move. Fix that before
going on stage."
    return $ok
}

cmd_baseline() {
    banner "0 — What is being served right now"
    note "One command. Point at 'connection', at the four agent configs, and at
the four flag values underneath. Nothing here is in the repo."
    plan baseline
    note "
Say: every number on that manifest is Python. The 4.4C gate is a constant, the
carrier pair is brute force over six options. What LaunchDarkly decided is the
four lines under 'capabilities' — and which model wrote the D1 findings."
}

cmd_kill() {
    banner "1 — pipeline-kill-switch  (operational, permanent)"
    note "The break-glass. Evaluated at A1, before any config fetch and before a
single ledger append, so an aborted run leaves no trace."
    flag "pipeline-kill-switch  →  ON"
    set +e
    uv run bbq-shipment-agent run plan \
        --recipients "$ROSTER" --ledger "$LEDGER" --operator "$OPERATOR" \
        2>&1 | tee "$OUT/killed.txt"
    local rc=${PIPESTATUS[0]}
    set -e
    printf '\n%s  exit %s%s\n' "$DIM" "$rc" "$RST"
    note "
Say: it stopped before it wrote anything. It also fails *open* — pull the
network and this run proceeds, because the system spends no money either way,
so a LaunchDarkly outage must not brick a shipping run."
    flag "pipeline-kill-switch  →  OFF"
}

cmd_validation() {
    banner "2 — validation-mode  (operational, permanent)"
    note "Not a boolean. Three values, and the interesting question is not
whether validation runs — it is who adjudicates an address the validator can
fix but should perhaps not fix silently."
    flag "validation-mode  →  standard"
    plan validation-standard
    note "
Point at: 'B2 corrected 1 address(es)' and Dev Okonkwo on the manifest.
Submitted ZIP was 60631. The validator returned 60613 — a different
neighbourhood, a different lane, a different rate. Standard applied it and
shipped."
    pause
    flag "validation-mode  →  strict"
    plan validation-strict
    banner "before → after"
    printf '  standard:\n'; summarize validation-standard
    printf '  strict:\n';   summarize validation-strict
    note "
Say: same code, same roster, same commit. One console change and that
recipient is now a human's problem instead of an assumption. That is the dial
you want on a first run against an unfamiliar list, and not on the fiftieth."
    flag "validation-mode  →  standard"
}

cmd_verify() {
    banner "3 — verification-enabled  (release, temporary until proven)"
    note "This one gates an agent — D1, a read-only critique of the finished
manifest, strictly downstream of it and unable to change it."
    flag "verification-enabled  →  OFF"
    plan verification-off
    note "
Point at the last line: D1 did not run. Note what is *not* there — no
invocation record. A stage that was skipped writes nothing; an invocation that
ran is always recorded, even when its reply could not be parsed."
    pause
    flag "verification-enabled  →  ON"
    plan verification-on
    note "
Point at: the findings, the model name, the token count, the iteration count.
The model came from LaunchDarkly too. Swapping Haiku for Sonnet here is a
console change, and the per-invocation metrics — tokens, duration, time to
first token, tool calls, success — go back to LaunchDarkly attributed to the
variation that served it."
}

cmd_ledger() {
    banner "4 — What the ledger kept"
    note "The closer. Every run above is reconstructible from committed JSONL
and nothing else — which matters more now that the values come from a console
rather than from a file in the repo."

    printf '\n%s  capability_evaluations.jsonl — one line per live evaluation%s\n\n' "$BOLD" "$RST"
    tail -12 "$LEDGER/capability_evaluations.jsonl" \
      | python3 -c '
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    run, stage = r["run_id"], r["stage"]
    flag, value, reason = r["flag"], r["value"], r["reason"]
    print(f"  {run}  {stage:<22} {flag:<21} {value:<9} {reason}")'

    printf '\n%s  runs.jsonl — the folded set each run operated under%s\n\n' "$BOLD" "$RST"
    tail -6 "$LEDGER/runs.jsonl" \
      | python3 -c '
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    snap = r.get("cap_snapshot")
    if not snap:
        continue
    caps = snap["capabilities"]
    run, fp = r["run_id"], r.get("cap_fingerprint", "")
    pl, va, ve = caps["planner"], caps["validation"], caps["verification"]
    print(f"  {run}  {fp}  planner={pl:<7} validation={va:<9} verification={ve}")'

    note "
Say: the stage each flag was evaluated under, the value LaunchDarkly served,
and whether it was LaunchDarkly or the offline fallback that served it. Months
later, a surprising run is diagnosable from this file alone. The fingerprint
names the set; every shipment row points back at it."
}

cmd_reset() {
    banner "Reset"
    printf '  Put these back in LaunchDarkly:\n\n'
    printf '    pipeline-kill-switch    OFF\n'
    printf '    validation-mode         standard\n'
    printf '    verification-enabled    ON\n'
    pause
    uv run bbq-shipment-agent run init --ledger "$LEDGER" --operator "$OPERATOR"
    note "
Check the kill switch line says 'off'. The three capability values are not
shown by 'run init' — they are evaluated when their stage runs — so confirm
them in the console or with a full 'demo/demo.sh 0'."
}

case "${1:-all}" in
    check)                cmd_check ;;
    0|baseline)           cmd_baseline ;;
    1|kill|killswitch)    cmd_kill ;;
    2|validation)         cmd_validation ;;
    3|verify|verification) cmd_verify ;;
    4|ledger)             cmd_ledger ;;
    reset)                cmd_reset ;;
    all)
        cmd_baseline; pause
        cmd_kill;     pause
        cmd_validation; pause
        cmd_verify;   pause
        cmd_ledger
        banner "Done"
        note "Run 'demo/demo.sh reset' to put the flags back."
        ;;
    *)
        # The header comment is the help text. Stop at the first line that is
        # not one, so adding to it never leaks `set -euo pipefail` onto stdout.
        awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
            "${BASH_SOURCE[0]}"
        exit 1 ;;
esac
