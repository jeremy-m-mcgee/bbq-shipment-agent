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
from pathlib import Path

from .agent_configs import (
    DEFAULT_SNAPSHOT_PATH,
    ChainedAgentConfigs,
    LaunchDarklyAgentConfigs,
    OfflineAgentConfigs,
    SnapshotAgentConfigs,
)
from .capabilities import (
    DEFAULT_CONFIG_PATH,
    CapabilityConfigError,
    KillSwitchEngaged,
    ValidationMode,
)
from .context import ContextError
from .ledger import RECORD_TYPES, LedgerCorruption, iter_records, rebuild, stream_path
from .plan import plan_run
from .planning import QuotingUnavailable, RecordedQuoter, ShippoQuoter, render
from .recipients import (
    DEFAULT_ROSTER_PATH,
    AddressValidationUnavailable,
    RecordedAddressValidator,
    RosterError,
    ShippoAddressValidator,
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


def _cmd_run_plan(args: argparse.Namespace) -> int:
    """A1 through C6: a roster in, a manifest out, no model calls.

    Build order step 3's deliverable. Exits non-zero when no carrier subset
    covers the run -- there is a plan to look at either way, but a partial one
    is not something to hand over as though it were complete.
    """
    roster = load_roster(args.recipients)
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
            packet_count=roster.packet_count,
        )
        _print_run(run, connection, _snapshot_state(args.snapshot, before), args.ledger)
    finally:
        if client is not None:
            client.close()

    mode = run.capabilities.validation
    print(f"  roster       {roster.source}  ({roster.packet_count} recipients)")
    print(f"  ship dates   {', '.join(d.isoformat() for d in roster.ship_dates)}")
    print(f"  validation   {mode.value}")
    print(f"  quotes       {args.quotes or 'live (Shippo)'}\n")

    result = plan_run(
        run,
        roster,
        ledger_root=args.ledger,
        quoter=_quoter(args),
        validator=_validator(args, mode),
    )

    if result.validation.corrected_count:
        print(f"B2 corrected {result.validation.corrected_count} address(es).")
    for excluded in result.escalated:
        print(f"escalated: {excluded.name} — {excluded.reason}")

    if result.manifest is None:
        print(f"\nno manifest: {result.reason}")
        _print_partial(result.solve)
        return 1

    print("\n" + render(result.manifest))
    if args.out:
        args.out.write_text(render(result.manifest) + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}")
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

    # Everything A1 needs, which both commands run.
    for sub in (init, plan):
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

    # `plan` takes its packet count from the roster rather than an argument:
    # the number is knowable, and a hand-typed one that disagrees with the file
    # would mis-target every flag evaluated against the run context.
    plan.add_argument(
        "--recipients",
        type=Path,
        default=DEFAULT_ROSTER_PATH,
        help=f"run input file (default: {DEFAULT_ROSTER_PATH})",
    )
    plan.add_argument(
        "--quotes",
        type=Path,
        default=None,
        help="replay recorded quotes instead of calling Shippo",
    )
    plan.add_argument(
        "--validations",
        type=Path,
        default=None,
        help="replay recorded address validations instead of calling Shippo",
    )
    plan.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"where live answers are cached (default: {DEFAULT_CACHE_DIR})",
    )
    # A 22-recipient run is ~300 quote calls, and UPS answers "Too Many
    # Requests" well before that on Shippo's shared master account. The
    # library defaults suit a single lane; a real run wants more patience,
    # and at three to five runs a year the extra minutes cost nothing.
    plan.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        help="quote attempts before a pinned carrier is called missing (default: 8)",
    )
    plan.add_argument(
        "--backoff",
        type=float,
        default=4.0,
        help="seconds between quote attempts, multiplied each time (default: 4)",
    )
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
        KillSwitchEngaged,
        QuotingUnavailable,
        RosterError,
    ) as exc:
        # These are operator errors with a fixable cause. A traceback buries
        # the message that says what to fix.
        print(f"error: {exc}")
        return 1
