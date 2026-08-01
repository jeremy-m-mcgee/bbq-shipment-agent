"""Command line entry point.

The ledger commands are build order step 1. `run init` is step 2, and it is
the only place the live LaunchDarkly path is actually assembled: everything
else in the package takes its provider and its config source by injection, so
without this command nothing ever opens a socket.

That is deliberate for the library and was a gap for the operator. Whether the
flags and AI Configs a run depends on actually exist in the LD environment is
not answerable from the tests, which run entirely on stubs. It is answerable
here.
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
from .capabilities import DEFAULT_CONFIG_PATH, CapabilityConfigError, KillSwitchEngaged
from .context import ContextError
from .ledger import RECORD_TYPES, LedgerCorruption, iter_records, rebuild, stream_path
from .run import (
    LaunchDarklyProvider,
    OfflineProvider,
    initialize_run,
    launchdarkly_client,
)

DEFAULT_LEDGER_ROOT = Path("ledger")
DEFAULT_DB_PATH = Path("ledger.duckdb")


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


def _cmd_run_init(args: argparse.Namespace) -> int:
    """A1, wired to whatever LaunchDarkly actually returns.

    The run record is written either way. An offline run is a real run under
    `baseline`, not a dry run, so pass a throwaway `--ledger` if all you want
    is to see what the environment serves.
    """
    client = None if args.offline else launchdarkly_client(timeout_seconds=args.timeout)
    try:
        if client is None:
            reason = _offline_reason(args.offline)
            connection = f"offline ({reason})"
            provider = OfflineProvider(reason=reason)
            # The snapshot is still consulted. Section 6.10 step 1 bootstraps
            # from cache, and a cached instruction set is exactly what makes an
            # unreachable run degrade rather than fail.
            agent_source = ChainedAgentConfigs(
                SnapshotAgentConfigs(args.snapshot), OfflineAgentConfigs(reason)
            )
        else:
            connection = "launchdarkly"
            provider = LaunchDarklyProvider(client)
            agent_source = ChainedAgentConfigs(
                LaunchDarklyAgentConfigs(client), SnapshotAgentConfigs(args.snapshot)
            )

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
        after = args.snapshot.read_bytes() if args.snapshot.exists() else None

        if after is None:
            state = f"{args.snapshot} not written (nothing usable retrieved)"
        elif before is None:
            state = f"{args.snapshot} created — commit it"
        elif before != after:
            state = f"{args.snapshot} changed — commit it"
        else:
            state = f"{args.snapshot} unchanged"

        _print_run(run, connection, state, args.ledger)
        return 0
    finally:
        # The SDK runs a background thread. Leaving it open hangs the CLI.
        if client is not None:
            client.close()


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
    init.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_ROOT)
    init.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    init.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    init.add_argument(
        "--profile", default=None, help="override default_profile for this run"
    )
    init.add_argument("--campaign", default=None, help="run context attribute")
    init.add_argument(
        "--packet-count", type=int, default=None, help="run context attribute"
    )
    init.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds to wait for the SDK to initialize (default: 5)",
    )
    init.add_argument(
        "--offline",
        action="store_true",
        help="skip LaunchDarkly entirely and take the cached/baseline path",
    )
    init.set_defaults(handler=_cmd_run_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (CapabilityConfigError, ContextError, KillSwitchEngaged) as exc:
        # These are operator errors with a fixable cause. A traceback buries
        # the message that says what to fix.
        print(f"error: {exc}")
        return 1
