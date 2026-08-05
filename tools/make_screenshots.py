"""Build B1's screenshot fixtures from specs, and keep the answer key in step.

    uv run python tools/make_screenshots.py --list
    uv run python tools/make_screenshots.py 08-imessage-cousins --dry-run
    uv run python tools/make_screenshots.py --all

A spec produces a PNG *and* its `ground_truth.json` entry, in one pass, from
one source. That is the point rather than a convenience: the delivered seven
have an answer key written by hand beside images made somewhere else, so
nothing but care keeps the two agreeing, and the regions in it were measured by
eye. Here the region is wherever the renderer put the glyphs.

Existing entries are merged, not replaced. The first seven screenshots have no
spec and cannot be regenerated -- they arrived as binaries -- so this only ever
rewrites entries it has a spec for and leaves the rest untouched.

What this does not do is record the downstream fixtures. A new screenshot needs
a B1 reply in `tests/fixtures/b1-extractions.json` and its addresses run
through the live validator into `tests/fixtures/shippo-addresses.json`, both of
which cost live calls and neither of which can be invented -- `RecordedVision`
and `RecordedAddressValidator` raise on an unknown input precisely so a fixture
cannot become a rubber stamp. `--next-steps` prints what is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from screenshot_fixtures.render import answer_key, render  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPECS = Path(__file__).parent / "screenshot_fixtures" / "specs"
SHOTS = ROOT / "tests" / "fixtures" / "screenshots"
TRUTH = SHOTS / "ground_truth.json"


def load_specs(names: list[str] | None) -> list[dict]:
    paths = sorted(SPECS.glob("*.yaml"))
    if names:
        wanted = {n.removesuffix(".yaml") for n in names}
        paths = [p for p in paths if p.stem in wanted]
        missing = wanted - {p.stem for p in paths}
        if missing:
            raise SystemExit(f"no spec named: {', '.join(sorted(missing))}")
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in paths]


def merge(entries: list[dict], dry_run: bool) -> tuple[int, int]:
    """Fold the new entries into the answer key, by filename."""
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    by_file = {s["file"]: s for s in truth["screenshots"]}
    added = sum(1 for e in entries if e["file"] not in by_file)
    for entry in entries:
        by_file[entry["file"]] = entry
    truth["screenshots"] = [by_file[k] for k in sorted(by_file)]
    if not dry_run:
        TRUTH.write_text(
            json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return added, len(entries) - added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="spec stems; omit with --all")
    parser.add_argument("--all", action="store_true", help="build every spec")
    parser.add_argument("--list", action="store_true", help="list the specs and stop")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="render and validate, writing neither the PNG nor the answer key",
    )
    args = parser.parse_args(argv)

    if args.list:
        for path in sorted(SPECS.glob("*.yaml")):
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
            people = ", ".join(p["name"] for p in spec.get("recipients", []))
            print(f"{path.stem:<28} {spec.get('theme','?'):<14} {people}")
        return 0

    if not args.names and not args.all:
        parser.error("name a spec, or pass --all")

    specs = load_specs(None if args.all else args.names)
    if not specs:
        print("nothing to build")
        return 0

    entries = []
    for spec in specs:
        rendered = render(spec)
        entry = answer_key(spec, rendered.regions)
        entries.append(entry)
        target = SHOTS / spec["file"]
        if not args.dry_run:
            rendered.image.save(target, "PNG")
        print(
            f"{'would build' if args.dry_run else 'built':<12} {spec['file']:<28} "
            f"{len(entry['recipients'])} recipient(s)"
        )
        for person in entry["recipients"]:
            r = person["region"]
            print(
                f"    {person['name']:<20} {person['difficulty']:<12} "
                f"region {r['x']},{r['y']} {r['width']}x{r['height']}"
            )
        for person in entry.get("non_recipients", []):
            r = person.get("region")
            where = f"region {r['x']},{r['y']} {r['width']}x{r['height']}" if r else "no address"
            print(f"    {person['name']:<20} {'not a recipient':<12} {where}")

    added, updated = merge(entries, args.dry_run)
    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {TRUTH.relative_to(ROOT)}: {added} added, {updated} rewritten")

    if not args.dry_run:
        print("\nStill needed before the suite will pass on these:")
        print("  1. A B1 reply per new image in tests/fixtures/b1-extractions.json")
        print("     (live vision call each; RecordedVision raises on an unknown image)")
        print("  2. Each new address through the live validator into")
        print("     tests/fixtures/shippo-addresses.json")
        print("     (RecordedAddressValidator raises rather than assuming clean)")
        print("  Neither can be hand-written. See tests/fixtures/screenshots/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
