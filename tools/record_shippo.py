"""Record the live Shippo answers a replayed screenshot run still asks for.

    uv run python tools/record_shippo.py --dry-run
    uv run python tools/record_shippo.py

`--extractions` and `--repairs` made B1 and B3 free to replay, and design 10
recorded what that did *not* buy: `shippo-quotes-sf-dc.json` holds one lane
against the twenty-odd destinations in `tests/fixtures/screenshots`, and
`shippo-addresses.json` holds no recording for the addresses B3 *proposed*.
So a full-depth screenshot replay stopped at C2, and the repair loop escalated
where a live run repaired -- `RecordedAddressValidator` raises on an unknown
address, `_adjudicate` catches that, and the proposal is rejected. Live: 6
repaired, 3 escalated. Replayed, before this: roughly 1 repaired, 8 escalated.

Both halves are one recording job, which is what this tool is. It is not a
third front-end: it *runs the CLI*, the same way `drive` posts at the UI, so
nothing here builds a `RunOptions`, touches a stage, or opens a socket of its
own. What it does is point `--cache` at a scratch directory seeded from the
fixtures, run the passes below, and fold the answers back.

## Why three passes and not one

The set B2 hands to B3 is a function of `validation-mode`, and the lanes C2
quotes are a function of what survives B2. No single run reaches both.

1. **strict** (`--profile full`). Every correctable address escalates, so B3
   is handed all of them and its proposals -- the tool calls it makes *and*
   the final ones `_adjudicate` re-validates -- go through the live validator.
   This is the pass that fixes the repair loop.
2. **standard** (`--profile planner_trial`). Corrections are applied rather
   than escalated, so C2 quotes the lane every one of them actually ships to.
   This is the pass that fixes the demo.
3. **the repaired lanes.** Pass 1 learns what B3's proposals canonicalise to,
   and those are different doorsteps -- one of them moves a Chicago recipient
   from 60631 to 60613. Nothing quotes them until `planner-mode` is `on`,
   which needs five completed shadow runs first (design 6.9), so this pass
   feeds them to the CLI as an ordinary roster instead of waiting for a run
   that can only happen on a real ledger.

Passes 1 and 2 overlap on the lanes that were clean all along; the scratch
cache means the overlap is free rather than re-quoted.

## What it does not record

Nothing about B1 or B3's *model* answers. Those are `b1-extractions.json` and
`b3-repairs.json`, they cost vision calls, and this tool replays them so a
re-record costs Shippo and nothing else. One consequence is visible in pass 1:
`b3-repairs.json` was captured against the seven screenshots that existed
then, so the recipient added with `08-imessage-cousins.png` gets no verdict
from the recording and stays escalated. That is the replay being honest about
a conversation it does not have, not a failure of this tool.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

SCREENSHOTS = FIXTURES / "screenshots"
ROSTER = FIXTURES / "roster-screenshots.yaml"
EXTRACTIONS = FIXTURES / "b1-extractions.json"
REPAIRS = FIXTURES / "b3-repairs.json"
COMPLETIONS = FIXTURES / "d1-completions.json"

#: Where the recordings land. The addresses file is shared with the roster
#: path and is merged into rather than replaced; the quotes file is new,
#: because "sf-dc" names the lane it holds and this one holds twenty others.
ADDRESSES_OUT = FIXTURES / "shippo-addresses.json"
QUOTES_OUT = FIXTURES / "shippo-quotes-screenshots.json"

#: Seeded from the fixtures, so re-recording after adding one screenshot pays
#: for that screenshot's lanes and nothing else. Under `.cache/`, which is
#: gitignored for exactly this reason: it is a cache of an API.
SCRATCH = ROOT / ".cache" / "recording"

#: The quoter's and validator's own cache filenames, which are the fixture
#: format already -- that is what makes a recording a copy rather than a
#: conversion.
QUOTE_CACHE = "shippo-quotes.json"
ADDRESS_CACHE = "shippo-addresses.json"


def cli_args(profile: str, roster: Path, cache: Path, ledger: Path) -> list[str]:
    """One `run plan`, live to Shippo and replayed everywhere else.

    `--offline` is about LaunchDarkly, not Shippo: the capability set comes
    from the committed profile so a recording does not depend on what a flag
    happened to serve, while `--quotes` and `--validations` are deliberately
    absent, which is what sends B2, B3 and C2 to the live API.
    """
    args = [
        "bbq-shipment-agent", "run", "plan",
        "--offline", "--profile", profile,
        "--recipients", str(roster),
        "--extractions", str(EXTRACTIONS),
        "--repairs", str(REPAIRS),
        "--completions", str(COMPLETIONS),
        "--cache", str(cache),
        "--ledger", str(ledger),
    ]
    if roster is ROSTER:
        args += ["--screenshots", str(SCREENSHOTS)]
    return args


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: dict) -> None:
    """The formatting the quoter and validator write, so a re-record diffs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def repaired_roster(addresses: dict, target: Path) -> tuple[Path | None, int]:
    """A roster of what B3's proposals validated to, for pass 3.

    Read out of `b3-repairs.json` rather than out of the run: the proposals
    are in the recorded conversation, and the canonical form of each is in the
    validator cache pass 1 just filled. A proposal the validator rejected has
    no lane worth quoting and is skipped -- C2 would never see it either.
    """
    recorded = json.loads(REPAIRS.read_text(encoding="utf-8"))
    text = recorded["turns"][-1].get("text", "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None, 0
    proposals = json.loads(text[start : end + 1]).get("repairs", []) or []

    rows = []
    for raw in proposals:
        proposed = raw.get("proposed") or {}
        street1, city = proposed.get("street1"), proposed.get("city")
        state, zip_code = proposed.get("state"), proposed.get("zip")
        if not all((street1, city, state, zip_code)):
            continue
        key = f"{street1}|{city}|{state}|{zip_code}|US".lower()
        row = addresses.get(key)
        if row is None or not row.get("is_valid"):
            continue
        rows.append(
            {
                "key": str(raw.get("recipient_key") or ""),
                "street1": row["street1"],
                "city": row["city"],
                "state": row["state"],
                "zip": row["zip"],
            }
        )
    if not rows:
        return None, 0

    lines = [
        "# Generated by tools/record_shippo.py, pass 3. Not committed.",
        "#",
        "# What B3's proposals validated to. Quoting them here records the lane",
        "# a run with `planner-mode: on` would ship them down, which no replayed",
        "# run can reach until five shadow runs exist to promote the planner.",
        "origin:",
        "  name: BBQ HQ",
        "  street1: 64 Divisadero St",
        "  city: San Francisco",
        "  state: CA",
        '  zip: "94117"',
        "ship_dates:",
        "  - 2026-08-17",
        "recipients:",
    ]
    for row in rows:
        lines += [
            f"  - key: {row['key'] or 'repaired'}",
            f"    name: {row['key'] or 'repaired'}",
            f"    street1: {row['street1']}",
            f"    city: {row['city']}",
            f"    state: {row['state']}",
            f'    zip: "{row["zip"]}"',
        ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target, len(rows)


def run(args: list[str], dry_run: bool) -> bool:
    print("\n  $ " + " ".join(args))
    if dry_run:
        return True
    completed = subprocess.run(args, cwd=ROOT)
    if completed.returncode != 0:
        print(f"  pass failed with exit code {completed.returncode}")
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the passes and what they would record, calling nothing",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="discard the scratch cache first, re-quoting every lane",
    )
    args = parser.parse_args(argv)

    quotes_before, addresses_before = load(QUOTES_OUT), load(ADDRESSES_OUT)
    print(f"  {QUOTES_OUT.relative_to(ROOT)}: {len(quotes_before)} quote(s)")
    print(f"  {ADDRESSES_OUT.relative_to(ROOT)}: {len(addresses_before)} address(es)")

    if args.fresh and SCRATCH.exists() and not args.dry_run:
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        # Seeded rather than empty: the answers already committed are answers,
        # and re-fetching them would spend calls to learn what is on disk.
        write(SCRATCH / QUOTE_CACHE, quotes_before)
        write(SCRATCH / ADDRESS_CACHE, addresses_before)

    with tempfile.TemporaryDirectory(prefix="bbq-record-") as tmp:
        ledger = Path(tmp) / "ledger"
        ok = True
        for profile, why in (
            ("full", "strict validation — B3 proposals through the live validator"),
            ("planner_trial", "standard validation — the lanes the demo plans on"),
        ):
            print(f"\n{profile}: {why}")
            ok = run(cli_args(profile, ROSTER, SCRATCH, ledger), args.dry_run) and ok

        print("\nrepaired lanes: what B3's proposals canonicalise to")
        roster, count = repaired_roster(load(SCRATCH / ADDRESS_CACHE), Path(tmp) / "repaired.yaml")
        if roster is None:
            print("  nothing to quote — pass 1 recorded no validated proposal.")
        else:
            print(f"  {count} repaired address(es)")
            ok = run(cli_args("baseline", roster, SCRATCH, ledger), args.dry_run) and ok

    if args.dry_run:
        print("\n  --dry-run: nothing was called and no fixture was written.")
        return 0

    quotes = {**quotes_before, **load(SCRATCH / QUOTE_CACHE)}
    addresses = {**addresses_before, **load(SCRATCH / ADDRESS_CACHE)}
    write(QUOTES_OUT, quotes)
    write(ADDRESSES_OUT, addresses)

    print(
        f"\n  {QUOTES_OUT.relative_to(ROOT)}: "
        f"{len(quotes)} quote(s), {len(quotes) - len(quotes_before)} new"
    )
    print(
        f"  {ADDRESSES_OUT.relative_to(ROOT)}: "
        f"{len(addresses)} address(es), {len(addresses) - len(addresses_before)} new"
    )
    if not ok:
        print("\n  A pass failed. The fixtures hold what did answer; re-run to finish.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
