"""Command line entry point.

The ledger commands are build order step 1. `run init` is step 2 and `run
plan` is step 3, and between them they are the only place the live paths are
assembled: everything else in the package takes its provider, its config
source, its quoter and its validator by injection, so without these commands
nothing ever opens a socket.

That is deliberate for the library and was a gap for the operator. Whether the
flags and AI Configs a run depends on actually exist in the LD environment is
not answerable from the tests, which run entirely on stubs. It is answerable
here, and so is whether a real recipient list produces a sensible manifest.
"""

from __future__ import annotations

import argparse
import os
import random
from pathlib import Path

from .agent_configs import (
    DEFAULT_SNAPSHOT_PATH,
    ChainedAgentConfigs,
    LaunchDarklyAgentConfigs,
    OfflineAgentConfigs,
    SnapshotAgentConfigs,
)
from .agents import (
    AnthropicModel,
    ModelUnavailable,
    RecordedModel,
)
from .capabilities import (
    DEFAULT_CONFIG_PATH,
    CapabilityConfigError,
    KillSwitchEngaged,
    ValidationMode,
    VerificationMode,
)
from .context import ContextError
from .ledger import RECORD_TYPES, LedgerCorruption, iter_records, rebuild, stream_path
from .plan import plan_run
from .planning import (
    DEFAULT_LANE,
    DEFAULT_LANES_PATH,
    LaneBook,
    LaneBookError,
    QuotingUnavailable,
    RecordedQuoter,
    ShippoQuoter,
    render,
)
from .recipients import (
    DEFAULT_ROSTER_PATH,
    AddressValidationUnavailable,
    ExtractionError,
    RecordedAddressValidator,
    RosterError,
    ShippoAddressValidator,
    extract_from_images,
    load_roster,
)
from .run import (
    LaunchDarklyProvider,
    OfflineProvider,
    initialize_run,
    launchdarkly_client,
)

DEFAULT_LEDGER_ROOT = Path("ledger")
DEFAULT_DB_PATH = Path("ledger.duckdb")
#: Cheap insurance against re-quoting an unchanged lane, and the reason a
#: second `run plan` on the same roster costs nothing. Gitignored: it is a
#: cache of a live API, not an artifact of the run.
DEFAULT_CACHE_DIR = Path(".cache")


def _cmd_rebuild(args: argparse.Namespace) -> int:
    connection = rebuild(args.ledger, args.db)
    try:
        print(f"rebuilt {args.db} from {args.ledger}")
        for record_type in RECORD_TYPES:
            stream = record_type.stream
            appends = connection.execute(
                f'SELECT count(*) FROM "{stream}_log"'
            ).fetchone()[0]
            entities = connection.execute(
                f'SELECT count(*) FROM "{stream}"'
            ).fetchone()[0]
            print(f"  {stream:<20} {appends:>6} appends  ->  {entities:>6} rows")
    finally:
        connection.close()
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Parse every line without touching the cache.

    Worth having separate from `rebuild`: this is the check you want to run in
    CI on the committed ledger, and it needs no database.
    """
    failed = False
    for record_type in RECORD_TYPES:
        path = stream_path(args.ledger, record_type)
        if not path.exists():
            print(f"  {record_type.stream:<20} (absent)")
            continue
        try:
            count = sum(1 for _ in iter_records(args.ledger, record_type))
        except LedgerCorruption as exc:
            print(f"  {record_type.stream:<20} CORRUPT: {exc}")
            failed = True
        else:
            print(f"  {record_type.stream:<20} {count:>6} appends ok")
    return 1 if failed else 0


def _offline_reason(requested: bool) -> str:
    """Say *why* the run is offline, not merely that it is.

    `launchdarkly_client` returns None for a missing key and for a client that
    did not come up, and those are different operator problems: one is a
    `.env` that was never filled in, the other is a key or a network that does
    not work. Section 6.10 treats both as normal, which makes distinguishing
    them the CLI's job rather than nobody's.
    """
    if requested:
        return "OFFLINE_REQUESTED"
    if not os.environ.get("LD_SDK_KEY"):
        return "NO_SDK_KEY"
    return "LD_UNREACHABLE"


def _print_run(run, connection: str, snapshot_state: str, ledger_root: Path) -> None:
    print(f"\n{run.run_id}\n")
    print(f"  connection   {connection}")
    print(f"  profile      {run.resolved.profile}")

    print("\n  capabilities")
    for name, value in run.capabilities.to_mapping().items():
        print(f"    {name:<14} {value:<14} {run.resolved.reasons[name]}")

    payload = run.payload
    print(f"\n  flags        {payload.source}")
    print(f"    proposed     {payload.overrides or '(nothing)'}")
    print(f"    reason       {payload.reason}")

    print("\n  agents")
    for key in sorted(run.agent_configs):
        config = run.agent_configs[key]
        status = "ready" if config.available else "unavailable"
        detail = ""
        if config.available:
            variation = config.variation_key or "?"
            detail = f"  {variation} rev {config.version}  {config.instruction_hash}"
            if config.model:
                detail += f"  {config.model}"
        print(f"    {key:<28} {status:<12} {config.source:<13} {config.reason}{detail}")

    print(f"\n  snapshot     {snapshot_state}")
    print(f"  ledger       {stream_path(ledger_root, RECORD_TYPES[0])}\n")


def _flag_sources(args: argparse.Namespace, client):
    """The provider and agent-config source for a run, live or offline.

    Shared by `run init` and `run plan` so the two cannot drift into
    disagreeing about what an offline run means.
    """
    if client is None:
        reason = _offline_reason(args.offline)
        # The snapshot is still consulted. Section 6.10 step 1 bootstraps
        # from cache, and a cached instruction set is exactly what makes an
        # unreachable run degrade rather than fail.
        return (
            f"offline ({reason})",
            OfflineProvider(reason=reason),
            ChainedAgentConfigs(
                SnapshotAgentConfigs(args.snapshot), OfflineAgentConfigs(reason)
            ),
        )
    return (
        "launchdarkly",
        LaunchDarklyProvider(client),
        ChainedAgentConfigs(
            LaunchDarklyAgentConfigs(client), SnapshotAgentConfigs(args.snapshot)
        ),
    )


def _snapshot_state(snapshot: Path, before: bytes | None) -> str:
    after = snapshot.read_bytes() if snapshot.exists() else None
    if after is None:
        return f"{snapshot} not written (nothing usable retrieved)"
    if before is None:
        return f"{snapshot} created — commit it"
    if before != after:
        return f"{snapshot} changed — commit it"
    return f"{snapshot} unchanged"


def _cmd_run_init(args: argparse.Namespace) -> int:
    """A1, wired to whatever LaunchDarkly actually returns.

    The run record is written either way. An offline run is a real run under
    `baseline`, not a dry run, so pass a throwaway `--ledger` if all you want
    is to see what the environment serves.
    """
    client = None if args.offline else launchdarkly_client(timeout_seconds=args.timeout)
    try:
        connection, provider, agent_source = _flag_sources(args, client)
        before = args.snapshot.read_bytes() if args.snapshot.exists() else None
        run = initialize_run(
            ledger_root=args.ledger,
            config_path=args.config,
            provider=provider,
            agent_source=agent_source,
            snapshot_path=args.snapshot,
            profile=args.profile,
            campaign=args.campaign,
            packet_count=args.packet_count,
        )
        _print_run(run, connection, _snapshot_state(args.snapshot, before), args.ledger)
        return 0
    finally:
        # The SDK runs a background thread. Leaving it open hangs the CLI.
        if client is not None:
            client.close()


def _lane_book(args: argparse.Namespace):
    """The committed ambient assumptions, or None to fall back to 22C.

    A missing file is not an error: `DEFAULT_LANE` still works and is what
    every test uses. It is worth saying out loud though, because one national
    ambient is what design 10 identified as collapsing the thermal envelope.
    """
    if not args.lanes.exists():
        print(
            f"note: {args.lanes} not found; every destination assumed "
            f"{DEFAULT_LANE.ambient_c}C. See design 10."
        )
        return None
    return LaneBook.load(args.lanes)


def _screenshots(args: argparse.Namespace) -> tuple[Path, ...]:
    """The .png files B1 reads: every one, or a named random sample.

    `--screenshot-count` exists to run B1 against part of a directory, which
    for `tests/fixtures/screenshots/` means a cheaper pass over the answer key
    than all seven images. Which subset was read is not incidental — extraction
    accuracy is per-image, so a run that read three of seven is not comparable
    to one that read a different three.

    So the sample is named rather than merely taken. The population is sorted
    before sampling, so the seed alone determines the choice regardless of
    directory order; the seed is printed whether it was supplied or generated;
    and the chosen filenames are printed too, because they are the account that
    survives a change of Python's sampling internals.

    Asking for more images than exist is an error rather than a clamp. A run
    silently reading seven when it was told ten looks exactly like a run that
    got what it asked for.
    """
    images = tuple(sorted(Path(args.screenshots).glob("*.png")))
    if not images:
        raise ExtractionError(f"no .png screenshots in {args.screenshots}")
    if args.screenshot_count is None:
        return images
    if args.screenshot_count < 1:
        raise ExtractionError(
            f"--screenshot-count must be at least 1, got {args.screenshot_count}"
        )
    if args.screenshot_count > len(images):
        raise ExtractionError(
            f"--screenshot-count {args.screenshot_count} exceeds the "
            f"{len(images)} .png file(s) in {args.screenshots}"
        )

    seed = args.screenshot_seed
    if seed is None:
        seed = random.randrange(2**32)
    sample = tuple(sorted(random.Random(seed).sample(images, args.screenshot_count)))
    print(
        f"  B1           sampling {len(sample)} of {len(images)} screenshot(s), "
        f"seed {seed}"
    )
    for image in sample:
        print(f"               {image.name}")
    return sample


def _roster(args: argparse.Namespace, run=None):
    """The run input, from the file and — when asked — from screenshots.

    The file always supplies the origin, the candidate ship dates and the lane
    declarations, because a screenshot says nothing about any of them. With
    `--screenshots`, B1 supplies the people and the file's `recipients` key
    becomes optional.
    """
    if not args.screenshots:
        if args.screenshot_count is not None or args.screenshot_seed is not None:
            raise ExtractionError(
                "--screenshot-count and --screenshot-seed select from "
                "--screenshots, which was not given. Nothing would sample."
            )
        return load_roster(args.recipients, lane_book=_lane_book(args))

    roster = load_roster(
        args.recipients, lane_book=_lane_book(args), require_recipients=False
    )
    images = _screenshots(args)

    print(f"  B1           reading {len(images)} screenshot(s)")
    extracted = extract_from_images(
        run, images, model=AnthropicModel(), ledger_root=args.ledger
    )
    print(f"               {extracted.describe()}")
    for u in extracted.unresolved:
        print(f"               no address: {u.name} — {u.note[:70]}")
    for name in extracted.unreadable:
        print(f"               UNREADABLE: {name}")

    # Lanes are assigned from the file's book, the same as for a hand-written
    # roster -- extraction produces an address, not an ambient assumption.
    book = _lane_book(args)
    recipients = extracted.recipients
    if book is not None and roster.ship_dates:
        from dataclasses import replace as _replace

        season = roster.ship_dates[0]
        recipients = tuple(
            _replace(r, lane=book.lane_for(r.address.state, season)) for r in recipients
        )
    return roster.with_recipients(recipients)


def _repairer(args: argparse.Namespace, planner):
    """B3's model, when `planner-mode` is not off and there are images."""
    from .capabilities import PlannerMode

    if planner is PlannerMode.OFF or not args.screenshots:
        return None
    return AnthropicModel()


def _quoter(args: argparse.Namespace):
    """Recorded quotes if asked for, live Shippo otherwise.

    There is no third option. A quoter that invents a rate when the API is
    unreachable would put a made-up cost on a manifest a human is about to
    approve, so both of these raise instead.
    """
    if args.quotes:
        return RecordedQuoter.from_file(args.quotes)
    args.cache.mkdir(parents=True, exist_ok=True)
    return ShippoQuoter(
        cache_path=args.cache / "shippo-quotes.json",
        max_attempts=args.max_attempts,
        backoff_s=args.backoff,
    )


def _validator(args: argparse.Namespace, mode: ValidationMode):
    """None when `validation-mode` is off, so nothing connects needlessly."""
    if mode is ValidationMode.OFF:
        return None
    if args.validations:
        return RecordedAddressValidator.from_file(args.validations)
    args.cache.mkdir(parents=True, exist_ok=True)
    return ShippoAddressValidator(cache_path=args.cache / "shippo-addresses.json")


def _verifier(args: argparse.Namespace, mode: VerificationMode):
    """None when `verification-enabled` is off, so nothing connects needlessly."""
    if mode is not VerificationMode.ON:
        return None
    if args.completions:
        return RecordedModel.from_file(args.completions)
    return AnthropicModel()


def _cmd_run_plan(args: argparse.Namespace) -> int:
    """A1 through C6, then D1: a roster in, a verified manifest out.

    Build order step 3's deliverable plus the agent step 2 left unwired.
    Exits non-zero when no carrier subset covers the run, or when D1 raised a
    blocker -- there is a plan to look at either way, but neither is something
    to hand over as though it were finished.
    """
    client = None if args.offline else launchdarkly_client(timeout_seconds=args.timeout)
    # Held open through planning rather than closed after A1: D1 reports its
    # metrics against the variation LaunchDarkly served, and that needs the
    # same client. Closing early would silently drop every AI Config metric.
    try:
        connection, provider, agent_source = _flag_sources(args, client)
        before = args.snapshot.read_bytes() if args.snapshot.exists() else None
        run = initialize_run(
            ledger_root=args.ledger,
            config_path=args.config,
            provider=provider,
            agent_source=agent_source,
            snapshot_path=args.snapshot,
            profile=args.profile,
            campaign=args.campaign,
        )
        _print_run(run, connection, _snapshot_state(args.snapshot, before), args.ledger)

        # After A1, because B1 needs the config A1 captured. The packet count
        # is therefore not a run-context attribute any more: it is not known
        # until extraction has run, and a guess would mis-target every flag.
        roster = _roster(args, run)
        mode = run.capabilities.validation
        verification = run.capabilities.verification
        print(f"  roster       {roster.source}  ({roster.packet_count} recipients)")
        print(f"  ship dates   {', '.join(d.isoformat() for d in roster.ship_dates)}")
        print(f"  validation   {mode.value}")
        print(f"  verification {verification.value}")
        print(f"  quotes       {args.quotes or 'live (Shippo)'}\n")

        result = plan_run(
            run,
            roster,
            ledger_root=args.ledger,
            quoter=_quoter(args),
            validator=_validator(args, mode),
            lane_book=_lane_book(args),
            repairer=_repairer(args, run.capabilities.planner),
            screenshots=args.screenshots,
            verifier=_verifier(args, verification),
        )
    finally:
        # The SDK runs a background thread. Leaving it open hangs the CLI.
        if client is not None:
            client.close()

    # Corrections only. The validator's advisory messages on a *clean* address
    # are captured on the result and printed nowhere -- design 10 records that
    # as open, along with what should be done about it.
    if result.validation.corrected_count:
        print(f"B2 corrected {result.validation.corrected_count} address(es).")
    if result.repair is not None:
        applied = "" if result.applied_repairs else "  (shadow: NOT applied)"
        print(f"B3  {result.repair.describe()}{applied}")
        for r in result.repair.repaired:
            print(f"    repaired {r.name}: {r.address.street1}, {r.address.city} "
                  f"{r.address.state} {r.address.zip}")
    for excluded in result.escalated:
        print(f"escalated: {excluded.name} — {excluded.reason}")
    # C4. Empty in the usual case; a recommendation, never an action.
    for remediation in result.remediations:
        print(f"C4  {remediation.describe()}")

    if result.manifest is None:
        print(f"\nno manifest: {result.reason}")
        _print_partial(result.solve)
        return 1

    print("\n" + render(result.manifest))
    blocked = _print_verification(result.verification)
    if args.out:
        args.out.write_text(render(result.manifest) + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 1 if blocked else 0


def _cmd_run_review(args: argparse.Namespace) -> int:
    """A1 through D2: plan a run, then review and record it.

    `run plan` stops at the manifest. This carries on into the conversation
    that ends in approved, approved with exclusions, or rejected -- which is
    now the last thing that happens to a run, since dispatch was removed.
    """
    client = None if args.offline else launchdarkly_client(timeout_seconds=args.timeout)
    try:
        connection, provider, agent_source = _flag_sources(args, client)
        before = args.snapshot.read_bytes() if args.snapshot.exists() else None
        run = initialize_run(
            ledger_root=args.ledger,
            config_path=args.config,
            provider=provider,
            agent_source=agent_source,
            snapshot_path=args.snapshot,
            profile=args.profile,
            campaign=args.campaign,
        )
        _print_run(run, connection, _snapshot_state(args.snapshot, before), args.ledger)

        roster = _roster(args, run)
        mode = run.capabilities.validation
        verification = run.capabilities.verification
        print(f"  roster       {roster.source}  ({roster.packet_count} recipients)")
        print(f"  validation   {mode.value}")
        print(f"  verification {verification.value}\n")

        result = plan_run(
            run,
            roster,
            ledger_root=args.ledger,
            quoter=_quoter(args),
            validator=_validator(args, mode),
            lane_book=_lane_book(args),
            repairer=_repairer(args, run.capabilities.planner),
            screenshots=args.screenshots,
            verifier=_verifier(args, verification),
        )
        if result.manifest is None:
            print(f"no manifest: {result.reason}")
            _print_partial(result.solve)
            return 1

        print(render(result.manifest))
        _print_verification(result.verification)
        return _review(args, run, roster, result, client)
    finally:
        if client is not None:
            client.close()


def _print_verification(verification) -> bool:
    """Print D1's report. Returns whether it raised a blocker.

    A blocker means a hard constraint is violated on a manifest that was about
    to be handed to a human for approval, so the command exits non-zero. The
    manifest is still printed: design 4 sends D1's output *to* the review, and
    hiding the plan would make the finding harder to act on, not easier.
    """
    if verification is None:
        return False

    print(f"\nD1  {verification.describe()}")
    if verification.ran:
        print(
            f"    {verification.model_requested} — "
            f"{verification.iterations} iteration(s), "
            f"{verification.input_tokens}+{verification.output_tokens} tokens"
        )
        if verification.model_drifted:
            # The ledger records what LaunchDarkly served. If that is not what
            # answered, say so here rather than leaving the record to imply it.
            print(
                f"    note: LaunchDarkly served {verification.model_requested}, "
                f"but {verification.model_responded} answered."
            )
    for finding in verification.findings:
        print(f"    {finding.describe()}")
        if finding.evidence:
            print(f"             evidence: {finding.evidence}")
    if verification.raw:
        print(f"    raw reply: {verification.raw[:400]}")
    return bool(verification.blockers)


def _review(args: argparse.Namespace, run, roster, result, client) -> int:
    """D2. Conversational review over the manifest, then a terminal state.

    Falls back to a plain prompt loop when `review-narrator` is unavailable.
    Design 6.10's posture: no instructions means no agent, and a manually
    read manifest is less helpful rather than less correct.
    """
    from .agents.narrator import Narrator, NarratorUnavailable
    from .review import Edit, EditKind, ReviewError, ReviewSession

    # The deduped set, not B2's output: reviewing a plan that still contains
    # a doorstep's second recipient would offer the operator edits on a
    # shipment the manifest never had.
    session = ReviewSession(
        run,
        result.suppression.eligible,
        roster.origin,
        roster.ship_dates,
        ledger_root=args.ledger,
        quoter=_quoter(args),
        escalated=result.validation.escalated,
        suppressed=result.suppression.suppressed,
    )

    narrator = None
    try:
        narrator = Narrator(
            run,
            session,
            ledger_root=args.ledger,
            model=AnthropicModel(),
        )
    except (NarratorUnavailable, ModelUnavailable) as exc:
        print(f"\nreview-narrator unavailable ({exc}). Reading the manifest directly.")

    if narrator is not None:
        print("\n" + narrator.open().reply)

    print(
        "\ncommands: approve | reject | exclude <key> | pin <key> <YYYY-MM-DD> | "
        "show | quit"
        + ("  (anything else goes to the narrator)" if narrator else "")
    )
    while session.terminal is None:
        try:
            said = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nleaving the review open; nothing was recorded.")
            return 1
        if not said:
            continue

        verb, *rest = said.split()
        try:
            if verb == "quit":
                print("leaving the review open; nothing was recorded.")
                return 1
            elif verb == "show":
                print(render(session.manifest) if session.manifest else "no plan")
            elif verb == "approve":
                print(f"\n{session.approve().value}")
            elif verb == "reject":
                print(f"\n{session.reject(' '.join(rest)).value}")
            elif verb == "confirm":
                print(session.confirm().describe())
            elif verb == "exclude" and rest:
                print(session.propose(Edit(EditKind.EXCLUDE, rest[0])).describe())
            elif verb == "pin" and len(rest) == 2:
                from datetime import date as _date

                print(
                    session.propose(
                        Edit(
                            EditKind.SHIP_DATE, rest[0], ship_date=_date.fromisoformat(rest[1])
                        )
                    ).describe()
                )
            elif narrator is not None:
                print("\n" + narrator.say(said).reply)
            else:
                print("unrecognised, and no narrator to ask.")
        except (ReviewError, ValueError) as exc:
            print(f"error: {exc}")
        except NarratorUnavailable as exc:
            print(f"narrator stopped: {exc}")
            narrator = None

    print(f"\nrecorded to {stream_path(args.ledger, RECORD_TYPES[0])}")
    return 0


def _print_partial(solve) -> None:
    """What the partially covering subsets would do, when none of them covers.

    C4 is build order step 11, so remediation is the operator's until then and
    this is what they need to do it: which subsets got closest, and who is
    stranded under each.
    """
    if solve.infeasible:
        print(f"feasible nowhere: {', '.join(solve.infeasible)}")
    if not solve.partial:
        print("no carrier subset produced a plan at all.")
        return
    print("\npartial plans, most coverage first")
    for plan in solve.partial[:5]:
        print(
            f"  {'+'.join(plan.carriers):<16} covers {plan.coverage:>3}  "
            f"${plan.total_cost:>9,.2f}  strands {', '.join(plan.stranded)}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbq-shipment-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ledger = subparsers.add_parser("ledger", help="ledger maintenance")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)

    for name, handler, help_text in (
        ("rebuild", _cmd_rebuild, "rebuild the derived DuckDB cache from JSONL"),
        ("verify", _cmd_verify, "parse the JSONL ledger and report any damage"),
    ):
        sub = ledger_sub.add_parser(name, help=help_text)
        sub.add_argument(
            "--ledger",
            type=Path,
            default=DEFAULT_LEDGER_ROOT,
            help=f"ledger directory (default: {DEFAULT_LEDGER_ROOT})",
        )
        if name == "rebuild":
            sub.add_argument(
                "--db",
                type=Path,
                default=DEFAULT_DB_PATH,
                help=f"derived cache path (default: {DEFAULT_DB_PATH})",
            )
        sub.set_defaults(handler=handler)

    run = subparsers.add_parser("run", help="pipeline runs")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    init = run_sub.add_parser(
        "init", help="A1: open a run against the live LaunchDarkly environment"
    )
    plan = run_sub.add_parser(
        "plan", help="A1 through C6: a recipient file in, a manifest out"
    )
    review = run_sub.add_parser(
        "review", help="A1 through D2: plan a run, then review and record it"
    )

    # Everything A1 needs, which all three commands run.
    for sub in (init, plan, review):
        sub.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_ROOT)
        sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        sub.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
        sub.add_argument(
            "--profile", default=None, help="override default_profile for this run"
        )
        sub.add_argument("--campaign", default=None, help="run context attribute")
        sub.add_argument(
            "--timeout",
            type=float,
            default=5.0,
            help="seconds to wait for the SDK to initialize (default: 5)",
        )
        sub.add_argument(
            "--offline",
            action="store_true",
            help="skip LaunchDarkly entirely and take the cached/baseline path",
        )

    init.add_argument(
        "--packet-count", type=int, default=None, help="run context attribute"
    )
    init.set_defaults(handler=_cmd_run_init)

    # `review` plans first, so it needs everything `plan` needs.
    for sub in (plan, review):
        sub.add_argument(
            "--recipients",
            type=Path,
            default=DEFAULT_ROSTER_PATH,
            help=f"run input file (default: {DEFAULT_ROSTER_PATH})",
        )
        sub.add_argument(
            "--quotes", type=Path, default=None,
            help="replay recorded quotes instead of calling Shippo",
        )
        sub.add_argument(
            "--validations", type=Path, default=None,
            help="replay recorded address validations instead of calling Shippo",
        )
        sub.add_argument(
            "--completions", type=Path, default=None,
            help="replay a recorded D1 reply instead of calling the model",
        )
        sub.add_argument(
            "--cache", type=Path, default=DEFAULT_CACHE_DIR,
            help=f"where live answers are cached (default: {DEFAULT_CACHE_DIR})",
        )
        sub.add_argument(
            "--max-attempts", type=int, default=8,
            help="quote attempts before a pinned carrier is called missing (default: 8)",
        )
        sub.add_argument(
            "--backoff", type=float, default=4.0,
            help="seconds between quote attempts, multiplied each time (default: 4)",
        )
        sub.add_argument(
            "--screenshots", type=Path, default=None,
            help="extract recipients from the .png files in this directory (B1). "
                 "The roster file still supplies origin, ship dates and lanes.",
        )
        sub.add_argument(
            "--screenshot-count", type=int, default=None,
            help="read a random sample of this many screenshots instead of the "
                 "whole directory. The sample is printed, and so is its seed.",
        )
        sub.add_argument(
            "--screenshot-seed", type=int, default=None,
            help="seed for --screenshot-count, to re-read the same sample. "
                 "Generated and printed when not supplied.",
        )
        sub.add_argument(
            "--lanes", type=Path, default=DEFAULT_LANES_PATH,
            help=f"ambient assumptions per destination (default: {DEFAULT_LANES_PATH})",
        )
    review.set_defaults(handler=_cmd_run_review)

    # Neither takes a packet count: the number is knowable from the roster,
    # and a hand-typed one that disagrees with the file would mis-target every
    # flag evaluated against the run context.
    plan.add_argument(
        "--out", type=Path, default=None, help="also write the manifest here"
    )
    plan.set_defaults(handler=_cmd_run_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (
        AddressValidationUnavailable,
        CapabilityConfigError,
        ContextError,
        ExtractionError,
        KillSwitchEngaged,
        LaneBookError,
        ModelUnavailable,
        QuotingUnavailable,
        RosterError,
    ) as exc:
        # These are operator errors with a fixable cause. A traceback buries
        # the message that says what to fix.
        print(f"error: {exc}")
        return 1
