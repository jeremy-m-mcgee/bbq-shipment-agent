"""Command line entry point.

The ledger commands are build order step 1. `run init` is step 2 and `run
plan` is step 3. `ui` serves the same pipeline from a browser.

This module used to be the only place the live paths were assembled. That
moved to `wiring.py` when the UI arrived, because two front-ends with two
copies of the wiring are two places for an offline fallback or a cache path to
drift. What is left here is argument parsing and rendering: everything below
still takes its provider, its config source, its quoter and its validator by
injection, so nothing in the library opens a socket.

That is deliberate for the library and was a gap for the operator. Whether the
flags and AI Configs a run depends on actually exist in the LD environment is
not answerable from the tests, which run entirely on stubs. It is answerable
here, and so is whether a real recipient list produces a sensible manifest.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .agent_configs import DEFAULT_SNAPSHOT_PATH
from .agents import ModelUnavailable
from .capabilities import (
    DEFAULT_CONFIG_PATH,
    CapabilityConfigError,
    KillSwitchEngaged,
)
from .context import ContextError
from .ledger import RECORD_TYPES, LedgerCorruption, iter_records, rebuild, stream_path
from .planning import (
    DEFAULT_LANES_PATH,
    LaneBookError,
    QuotingUnavailable,
    render,
)
from .recipients import (
    DEFAULT_ROSTER_PATH,
    AddressValidationUnavailable,
    ExtractionError,
    RosterError,
    to_shipments,
)
from .run import record_run_reasons
from .wiring import (
    DEFAULT_CACHE_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_LEDGER_ROOT,
    PrintProgress,
    RunOptions,
    ScreenshotSelection,
    build_roster,
    missing_credentials,
    open_run,
    plan_with,
    quoter,
)

#: Where `ui` looks for screenshots unless told otherwise. The fixture set is
#: the only directory this repo is guaranteed to have, and a picker with
#: nothing to pick is a poor first screen.
DEFAULT_SCREENSHOT_DIR = Path("tests/fixtures/screenshots")


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


def _cmd_tools(args: argparse.Namespace) -> int:
    """Which tools each agent was offered, and which it actually called.

    Reads the derived cache in memory rather than the committed one on disk,
    so this answers from the JSONL and cannot report something a stale
    `ledger.duckdb` is holding and the source of truth is not.

    Offered is printed even when nothing was called, because that is the
    reading the pair exists to support: an agent that was handed three tools
    and used none is a finding, and an agent that was handed none because the
    run had no validator is a configuration.
    """
    connection = rebuild(args.ledger)
    try:
        rows = connection.execute(
            "SELECT run_id, agent_key, tools_offered, tools_called "
            "FROM agent_invocations ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()

    if args.run:
        rows = [row for row in rows if row[0].startswith(args.run)]
        if not rows:
            print(f"no agent invocations for run {args.run}")
            return 1

    if not rows:
        print("no agent invocations recorded")
        return 0

    for run_id in dict.fromkeys(row[0] for row in rows):
        run_rows = [row for row in rows if row[0] == run_id]
        print(f"\nrun {run_id}  ({len(run_rows)} invocations)")
        for agent_key in sorted({row[1] for row in run_rows}):
            agent_rows = [row for row in run_rows if row[1] == agent_key]
            offered: list[str] = []
            calls: dict[str, int] = {}
            for _, _, row_offered, row_called in agent_rows:
                for name in row_offered or ():
                    if name not in offered:
                        offered.append(name)
                for name in row_called or ():
                    calls[name] = calls.get(name, 0) + 1

            # An invocation that recorded neither column has no tool loop at
            # all -- B1 and D1 -- which is a different statement from one that
            # was offered nothing, and the line should not blur them.
            tracked = [row for row in agent_rows if row[2] is not None]
            if not tracked:
                summary = "no tool loop"
            elif not offered:
                summary = "offered nothing"
            else:
                summary = "offered " + ", ".join(offered)
            print(f"  {agent_key:<24} {len(agent_rows):>3} inv   {summary}")
            continuation = " " * len(f"  {'':<24} {'':>3} inv   ")
            if calls:
                used = "  ".join(f"{name} x{n}" for name, n in sorted(calls.items()))
                print(f"{continuation}called {used}")
            elif offered:
                print(f"{continuation}called nothing")
    return 0


def _options(args: argparse.Namespace) -> RunOptions:
    """A parsed command line as the options every front-end shares.

    The one place `argparse` meets `wiring`. Everything past this point is
    keyed on `RunOptions`, which is what lets a browser form run the same
    pipeline without inventing a Namespace to satisfy it.
    """
    selection = None
    count = getattr(args, "screenshot_count", None)
    seed = getattr(args, "screenshot_seed", None)
    if count is not None or seed is not None:
        selection = ScreenshotSelection(count=count, seed=seed)
    return RunOptions(
        ledger=args.ledger,
        config=args.config,
        snapshot=args.snapshot,
        lanes=getattr(args, "lanes", DEFAULT_LANES_PATH),
        cache=getattr(args, "cache", DEFAULT_CACHE_DIR),
        recipients=getattr(args, "recipients", DEFAULT_ROSTER_PATH),
        profile=args.profile,
        campaign=args.campaign,
        packet_count=getattr(args, "packet_count", None),
        offline=args.offline,
        timeout=args.timeout,
        screenshots=getattr(args, "screenshots", None),
        selection=selection,
        quotes=getattr(args, "quotes", None),
        validations=getattr(args, "validations", None),
        completions=getattr(args, "completions", None),
        extractions=getattr(args, "extractions", None),
        repairs=getattr(args, "repairs", None),
        max_attempts=getattr(args, "max_attempts", 8),
        backoff=getattr(args, "backoff", 2.5),
    )


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


def _cmd_run_init(args: argparse.Namespace) -> int:
    """A1, wired to whatever LaunchDarkly actually returns.

    The run record is written either way. An offline run is a real run under
    `baseline`, not a dry run, so pass a throwaway `--ledger` if all you want
    is to see what the environment serves.
    """
    context = open_run(_options(args), PrintProgress())
    try:
        _print_run(context.run, context.connection, context.snapshot, args.ledger)
        return 0
    finally:
        # The SDK runs a background thread. Leaving it open hangs the CLI.
        if context.client is not None:
            context.client.close()



def _cmd_run_extract(args: argparse.Namespace) -> int:
    """A1 and B1, then stop. Nothing downstream of extraction runs.

    B1 is the stage worth iterating on alone: design 6.2 makes it the only one
    with a ground-truth answer key, and 6.6 now retrieves its config per image,
    so which variation read which screenshot is the thing you change and
    re-check. Everything after it is expensive for reasons that have nothing to
    do with extraction -- C2 quotes every configuration of every shipment
    against a live carrier API, which is minutes of waiting to learn nothing
    about a prompt.

    No Shippo call happens here: B2 is downstream. So this is also the run to
    reach for when only `ANTHROPIC_API_KEY` is set.

    The run record is still written, because A1 really did open a run and the
    ledger is append-only. Pass a throwaway `--ledger` when iterating.
    """
    options = _options(args)
    if options.screenshots is None:
        print("run extract needs --screenshots; there is nothing for B1 to read.")
        return 2

    context = open_run(options, PrintProgress())
    try:
        _print_run(context.run, context.connection, context.snapshot, args.ledger)
        # B1 runs inside `build_roster` and nothing after it does, which is
        # what makes this a seam rather than a special case.
        roster = build_roster(options, context.run, context.images, PrintProgress())
        # `plan_with` records this on the way through planning; this command
        # stops before planning, so it has to do it itself or the hash on
        # every invocation record names a file the ledger cannot resolve.
        record_run_reasons(options.ledger, context.run, context.extra_reasons)
    finally:
        # The SDK runs a background thread. Leaving it open hangs the CLI.
        if context.client is not None:
            context.client.close()

    print(f"\nB1 read {len(context.images)} screenshot(s):")
    for image in context.images:
        key = context.extra_reasons.get("screenshot_keys", {}).get(image.name, "?")
        config = context.run.image_configs.get(key)
        variation = config.variation_key if config else "?"
        model = (config.model if config else None) or "?"
        print(f"  {image.name:<28} {key}  {variation}  {model}")

    print(f"\n{roster.packet_count} recipient(s):")
    for recipient in roster.recipients:
        address = recipient.address
        confidence = "" if recipient.confidence is None else f"  ({recipient.confidence:.2f})"
        source = recipient.provenance.source_image if recipient.provenance else "?"
        print(
            f"  {recipient.name:<22} {address.street1}, {address.city} "
            f"{address.state} {address.zip}{confidence}  [{source}]"
        )
    return 0


def _cmd_run_plan(args: argparse.Namespace) -> int:
    """A1 through C6, then D1: a roster in, a verified manifest out.

    Build order step 3's deliverable plus the agent step 2 left unwired.
    Exits non-zero when no carrier subset covers the run, or when D1 raised a
    blocker -- there is a plan to look at either way, but neither is something
    to hand over as though it were finished.
    """
    options = _options(args)
    # The client is held open through planning rather than closed after A1: D1
    # reports its metrics against the variation LaunchDarkly served, and that
    # needs the same client. Closing early would silently drop every AI Config
    # metric.
    context = open_run(options, PrintProgress())
    try:
        _print_run(context.run, context.connection, context.snapshot, args.ledger)
        # Planning runs after A1, because B1 needs the config A1 captured. The
        # packet count is therefore not a run-context attribute: it is not
        # known until extraction has run, and a guess would mis-target every
        # flag.
        plan_with(context, options, PrintProgress())
        print()
        result = context.result
    finally:
        # The SDK runs a background thread. Leaving it open hangs the CLI.
        if context.client is not None:
            context.client.close()

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
    options = _options(args)
    context = open_run(options, PrintProgress())
    try:
        _print_run(context.run, context.connection, context.snapshot, args.ledger)
        plan_with(context, options, PrintProgress())
        print()
        run, roster, result = context.run, context.roster, context.result
        if result.manifest is None:
            print(f"no manifest: {result.reason}")
            _print_partial(result.solve)
            return 1

        print(render(result.manifest))
        _print_verification(result.verification)
        return _review(args, options, run, roster, result)
    finally:
        if context.client is not None:
            context.client.close()


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


def _review(args: argparse.Namespace, options: RunOptions, run, roster, result) -> int:
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
    #
    # Converted through `to_shipments`, because `suppression.eligible` is
    # phase B's `Recipient` and D2 solves over phase C's `Shipment`. This
    # crossed the B4 -> C1 boundary untranslated and `run review` died on
    # `Recipient has no attribute recipient_key` the first time anyone drove
    # the conversation end to end -- the tests built their session from
    # `to_shipments(...)` by hand, so the seam the CLI actually uses was the
    # one thing not covered.
    session = ReviewSession(
        run,
        to_shipments(result.suppression.eligible),
        roster.origin,
        roster.ship_dates,
        ledger_root=args.ledger,
        quoter=quoter(options),
        escalated=result.validation.escalated,
        suppressed=result.suppression.suppressed,
    )

    narrator = None
    try:
        from .agents import AnthropicModel

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


def _warn_about_credentials(options: RunOptions) -> None:
    """Name the keys the live stages will reach for and not find.

    Not an abort. A fully replayed launch needs no keys at all, and someone
    who only wants to look at the page should not be stopped from doing it.
    """
    missing = missing_credentials(options)
    if not missing:
        return
    print("\n  WARNING — these runs will fail:")
    for key, stages in sorted(missing.items()):
        print(f"    {key:<20} unset or empty, needed by {', '.join(stages)}")
    print(
        "\n  If the key is in .env, the process did not load it. `uv run` reads\n"
        "  that file only when UV_ENV_FILE points at it — the devcontainer sets\n"
        "  it, a plain shell may not. Restart with:\n\n"
        "    UV_ENV_FILE=$PWD/.env uv run bbq-shipment-agent ui"
    )


def _cmd_ui(args: argparse.Namespace) -> int:
    """Serve the planning UI on the loopback interface.

    Bound to 127.0.0.1 and there is deliberately no option to change it. The
    page serves `recipients.yaml`, which holds real home addresses, and
    screenshots of people's messages. There is no authentication because the
    answer to "who can reach this" is "processes on this machine", and an
    interface flag would quietly turn that into a different answer.
    """
    import uvicorn

    from .ui import create_app

    options = _options(args)
    app = create_app(options, screenshot_dir=args.screenshots)
    print(f"\n  bbq-shipment-agent ui   http://127.0.0.1:{args.port}")
    print(f"  ledger                  {args.ledger}")
    print(f"  screenshots             {args.screenshots}")
    print(f"  replay                  {'yes' if options.replaying else 'no (live calls)'}")

    # Said here rather than discovered on the worker thread. A server that
    # starts cleanly implies it is ready to run, and without this the first
    # sign of a missing key is a failed run several clicks later -- which the
    # CLI never had, because it fails on the way to the first stage.
    _warn_about_credentials(options)
    print("\n  ctrl-c to stop\n", flush=True)
    # Flushed explicitly: stdout is block-buffered when it is not a terminal,
    # so a banner printed before `uvicorn.run` blocks would sit unseen in the
    # buffer for the life of the server. Which is exactly the case where the
    # warning above matters most -- someone piping the log to a file.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbq-shipment-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ledger = subparsers.add_parser("ledger", help="ledger maintenance")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)

    for name, handler, help_text in (
        ("rebuild", _cmd_rebuild, "rebuild the derived DuckDB cache from JSONL"),
        ("verify", _cmd_verify, "parse the JSONL ledger and report any damage"),
        ("tools", _cmd_tools, "which tools each agent was offered and called"),
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
        if name == "tools":
            sub.add_argument(
                "--run",
                default="",
                help="only this run id (a leading prefix is enough)",
            )
        sub.set_defaults(handler=handler)

    run = subparsers.add_parser("run", help="pipeline runs")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    init = run_sub.add_parser(
        "init", help="A1: open a run against the live LaunchDarkly environment"
    )
    extract = run_sub.add_parser(
        "extract", help="A1 and B1 only: read screenshots, print what was found"
    )
    plan = run_sub.add_parser(
        "plan", help="A1 through C6: a recipient file in, a manifest out"
    )
    review = run_sub.add_parser(
        "review", help="A1 through D2: plan a run, then review and record it"
    )
    ui = subparsers.add_parser(
        "ui", help="serve the planning UI on 127.0.0.1: pick screenshots, then plan"
    )
    ui.add_argument("--port", type=int, default=8765)
    ui.set_defaults(handler=_cmd_ui)

    # Everything A1 needs, which all four commands run.
    for sub in (init, extract, plan, review, ui):
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
    extract.set_defaults(handler=_cmd_run_extract)

    # `review` plans first, so it needs everything `plan` needs, and the UI
    # plans too -- its form supplies only the screenshot selection and the
    # profile, so every other input still arrives as a command-line default.
    for sub in (extract, plan, review, ui):
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
        # B1 and B3 were the two live model paths with no replay, which meant
        # the screenshot route could not be exercised without paying for
        # vision calls -- even though both fixtures existed.
        sub.add_argument(
            "--extractions", type=Path, default=None,
            help="replay recorded B1 extractions instead of calling the vision model",
        )
        sub.add_argument(
            "--repairs", type=Path, default=None,
            help="replay a recorded B3 repair conversation instead of calling the model",
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
            "--backoff", type=float, default=2.5,
            help="seconds between quote attempts, multiplied each time (default: 2.5)",
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
    # The UI's whole point is choosing images, so it starts pointed at the
    # fixture set rather than at nothing. Override with `--screenshots` for a
    # real run; pass a directory with no .png files and the picker says so and
    # falls back to the roster file.
    ui.set_defaults(screenshots=DEFAULT_SCREENSHOT_DIR)

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
