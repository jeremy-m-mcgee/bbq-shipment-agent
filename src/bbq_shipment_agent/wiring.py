"""Where the live paths are assembled. Everything else takes them injected.

`cli.py` used to say this about itself, and it was true while the CLI was the
only front-end. It is not any more: `ui/` runs the same pipeline from a
browser, and two copies of the wiring would be two places for an offline
fallback or a cache path to drift.

So the rule moves rather than weakens. **This module is the only place a
socket-opening object is constructed** -- the LaunchDarkly client, the Shippo
quoter, the Shippo validator, the Anthropic model. The library below it still
takes every one of them by injection, so no test can open a socket by
importing something.

## Options, not a Namespace

The helpers were keyed on `argparse.Namespace`, which a web form does not
have. `RunOptions` is the same set of values with a name, so the CLI builds
one from its parsed arguments and the UI builds one from a POST body, and
neither knows how the other did it.

## Progress is a parameter, not a print

The helpers printed as they went, which is right for a CLI and useless to
anything else. They now emit through `Progress`, and the caller decides
whether that becomes a line on a terminal or an event on a stream. The CLI's
output is unchanged.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

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
    RecordedConversation,
    RecordedModel,
    RecordedVision,
)
from .capabilities import (
    LaunchDarklyGate,
    OfflineGate,
    PlannerMode,
    ValidationMode,
    VerificationMode,
)
from .context import ImageIdentity
from .operators import DEFAULT_OPERATORS_PATH, OperatorPool
from .planning import (
    DEFAULT_LANE,
    DEFAULT_LANES_PATH,
    LaneBook,
    RecordedQuoter,
    ShippoQuoter,
)
from .recipients import (
    DEFAULT_ROSTER_PATH,
    ExtractionError,
    RecordedAddressValidator,
    ShippoAddressValidator,
    extract_from_images,
    load_roster,
)
from .run import (
    initialize_run,
    launchdarkly_client,
    record_run_reasons,
)

DEFAULT_LEDGER_ROOT = Path("ledger")
DEFAULT_DB_PATH = Path("ledger.duckdb")
#: Cheap insurance against re-quoting an unchanged lane, and the reason a
#: second plan on the same roster costs nothing. Gitignored: it is a cache of
#: a live API, not an artifact of the run.
DEFAULT_CACHE_DIR = Path(".cache")


class Progress(Protocol):
    """Where a stage says what it is doing.

    One method, because the callers want different things from it and none of
    them wants a logging framework. `stage` is the pipeline stage ("B1"), and
    the keyword fields are for a consumer that renders structure rather than
    text -- the CLI ignores them and prints the message.
    """

    def emit(self, stage: str, message: str, **fields: Any) -> None: ...


class NullProgress:
    """Says nothing. The default, so a library caller need not care."""

    def emit(self, stage: str, message: str, **fields: Any) -> None:
        return None


class PrintProgress:
    """The CLI's rendering: a padded stage column, then the message.

    Continuation lines pass an empty stage and land under the column, which is
    how `run plan` already printed a screenshot sample.
    """

    def emit(self, stage: str, message: str, **fields: Any) -> None:
        print(f"  {stage:<13}{message}" if stage else f"               {message}")


class RunDepth(StrEnum):
    """How far down the pipeline a run goes.

    Two values, because there is exactly one honest seam and `run extract`
    already found it: B1 runs inside `build_roster` and nothing after it does,
    so stopping there is a place the pipeline naturally ends rather than a
    stage counter to keep in step with design 4.

    It is a real distinction rather than a convenience. `extract` costs one
    vision call per image and touches no carrier API; `plan` quotes every
    configuration of every shipment, which is minutes of waiting that says
    nothing about a prompt. A front-end iterating on B1 wants the first and
    gets charged for the second.
    """

    PLAN = "plan"
    EXTRACT = "extract"


@dataclass(frozen=True)
class ScreenshotSelection:
    """Which images B1 reads: named ones, or a seeded sample of *n*.

    Two ways in, because the two front-ends can ask different questions. A CLI
    cannot show you an iMessage thread, so it offers a count and prints the
    sample it took; a browser can, so it offers checkboxes. Both resolve to a
    named tuple of files, and the names are what reaches the ledger -- with an
    explicit choice there is no seed, so the filenames are the *only* account
    of what B1 read.

    Explicit and count together is an error rather than a precedence rule. A
    front-end that sent both has a bug, and picking one silently would hide it.
    """

    explicit: tuple[str, ...] | None = None
    count: int | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        if self.explicit is not None and self.count is not None:
            raise ExtractionError(
                "a screenshot selection is either explicit filenames or a "
                "count, not both."
            )
        if self.explicit is None and self.count is None and self.seed is not None:
            raise ExtractionError(
                "a screenshot seed selects from a count, and no count was given."
            )


@dataclass(frozen=True)
class RunOptions:
    """Everything a run needs that is not the pipeline itself.

    Frozen, so a front-end holding one cannot have it changed underneath it by
    a stage. `replace` is the way to vary one for a second run.
    """

    ledger: Path = DEFAULT_LEDGER_ROOT
    snapshot: Path = DEFAULT_SNAPSHOT_PATH
    lanes: Path = DEFAULT_LANES_PATH
    cache: Path = DEFAULT_CACHE_DIR
    recipients: Path = DEFAULT_ROSTER_PATH
    #: The committed user/department pool. A path rather than the pool itself
    #: for the same reason `lanes` is: a front-end holds options, not loaded
    #: configuration, and the file is read once the run opens.
    operators: Path = DEFAULT_OPERATORS_PATH
    profile: str | None = None
    #: Which key from `operators` this run presents itself as. A key and never
    #: a pair: the department is looked up, so a form cannot claim one. None
    #: takes the pool's `default_operator`, which may itself be nobody.
    operator: str | None = None
    campaign: str | None = None
    #: A run context attribute, and only `run init` supplies one. Planning
    #: knows the real count from the roster, and a hand-typed one that
    #: disagreed would mis-target every flag evaluated against the context.
    packet_count: int | None = None
    offline: bool = False
    timeout: float = 5.0
    #: B1's input directory. None means the roster file supplies the people.
    screenshots: Path | None = None
    selection: ScreenshotSelection | None = None
    #: Where the run stops. The CLI says it with a subcommand and the form
    #: says it with a field; both arrive here, so neither front-end has its
    #: own idea of what "extract only" means.
    depth: RunDepth = RunDepth.PLAN
    #: Replay files. Each one is a live path not taken.
    quotes: Path | None = None
    validations: Path | None = None
    completions: Path | None = None
    extractions: Path | None = None
    repairs: Path | None = None
    max_attempts: int = 8
    backoff: float = 2.5

    @property
    def replaying(self) -> bool:
        """Whether every live path this run would take has a recording."""
        return all(
            (
                self.quotes is not None,
                self.validations is not None,
                self.completions is not None,
                self.extractions is not None or self.screenshots is None,
                self.repairs is not None or self.screenshots is None,
            )
        )


def offline_reason(requested: bool) -> str:
    """Say *why* the run is offline, not merely that it is.

    `launchdarkly_client` returns None for a missing key and for a client that
    did not come up, and those are different operator problems: one is a
    `.env` that was never filled in, the other is a key or a network that does
    not work. Section 6.10 treats both as normal, which makes distinguishing
    them the front-end's job rather than nobody's.
    """
    if requested:
        return "OFFLINE_REQUESTED"
    if not os.environ.get("LD_SDK_KEY"):
        return "NO_SDK_KEY"
    return "LD_UNREACHABLE"


#: Which environment key each live path reaches for, and what to call the
#: path when telling the operator it will fail.
_CREDENTIALS = (
    ("ANTHROPIC_API_KEY", "extractions", "B1 extraction", True),
    ("ANTHROPIC_API_KEY", "repairs", "B3 repair", True),
    ("ANTHROPIC_API_KEY", "completions", "D1 verification", False),
    ("SHIPPO_API_KEY", "quotes", "C2 rate quotes", False),
    ("SHIPPO_API_KEY", "validations", "B2 address validation", False),
)


def missing_credentials(options: RunOptions) -> dict[str, tuple[str, ...]]:
    """Live paths this run may take that have no key to take them with.

    A warning rather than a verdict, and the distinction is worth keeping.
    Capabilities are evaluated live when each stage runs, so `verification-
    enabled` may be off and D1 may never ask; `validation-mode` may be off and
    B2 likewise. What this can say without guessing is narrower and still
    useful: a stage with no recording will reach for a key, and it is not there.

    Empty counts as unset. `.env` is seeded from `.env.example`, so an
    unconfigured key is present-but-empty rather than absent -- and the most
    common cause of all is a `.env` that was never loaded, because `uv run`
    reads it only when `UV_ENV_FILE` points at it.
    """
    missing: dict[str, list[str]] = {}
    for key, recording, stage, needs_screenshots in _CREDENTIALS:
        if getattr(options, recording) is not None:
            continue  # replayed, so nothing live is reached for
        if needs_screenshots and options.screenshots is None:
            continue  # B1 and B3 only run when there are images
        if os.environ.get(key):
            continue
        missing.setdefault(key, []).append(stage)
    return {key: tuple(stages) for key, stages in missing.items()}


def flag_sources(options: RunOptions, client: Any) -> tuple[str, Any, Any]:
    """The flag gate and agent-config source for a run, live or offline.

    Shared by every entry point so they cannot drift into disagreeing about
    what an offline run means. The gate is the live capability seam: online it
    evaluates each flag against LaunchDarkly under its stage context, offline it
    serves the per-capability defaults.
    """
    if client is None:
        reason = offline_reason(options.offline)
        # The snapshot is still consulted. Design 6.10 step 1 bootstraps from
        # cache, and a cached instruction set is exactly what makes an
        # unreachable run degrade rather than fail.
        return (
            f"offline ({reason})",
            OfflineGate(reason=reason),
            ChainedAgentConfigs(
                SnapshotAgentConfigs(options.snapshot), OfflineAgentConfigs(reason)
            ),
        )
    return (
        "launchdarkly",
        LaunchDarklyGate(client),
        ChainedAgentConfigs(
            LaunchDarklyAgentConfigs(client), SnapshotAgentConfigs(options.snapshot)
        ),
    )


def snapshot_state(snapshot: Path, before: bytes | None) -> str:
    after = snapshot.read_bytes() if snapshot.exists() else None
    if after is None:
        return f"{snapshot} not written (nothing usable retrieved)"
    if before is None:
        return f"{snapshot} created — commit it"
    if before != after:
        return f"{snapshot} changed — commit it"
    return f"{snapshot} unchanged"


def lane_book(options: RunOptions, progress: Progress | None = None) -> Any:
    """The committed ambient assumptions, or None to fall back to 22C.

    A missing file is not an error: `DEFAULT_LANE` still works and is what
    every test uses. It is worth saying out loud though, because one national
    ambient is what design 10 identified as collapsing the thermal envelope.
    """
    if not options.lanes.exists():
        if progress is not None:
            progress.emit(
                "lanes",
                f"note: {options.lanes} not found; every destination assumed "
                f"{DEFAULT_LANE.ambient_c}C. See design 10.",
            )
        return None
    return LaneBook.load(options.lanes)


def available_screenshots(directory: Path | str) -> tuple[Path, ...]:
    """Every .png in a directory, sorted.

    Sorted before anything else sees it, so a seeded sample depends on the
    seed alone rather than on the order the filesystem happened to return.
    """
    return tuple(sorted(Path(directory).glob("*.png")))


def resolve_screenshots(
    options: RunOptions, progress: Progress | None = None
) -> tuple[Path, ...]:
    """The .png files B1 reads: every one, a named sample, or named files.

    Which subset was read is not incidental -- extraction accuracy is
    per-image, so a run that read three of seven is not comparable to one that
    read a different three. The sample is therefore *named* rather than merely
    taken: the seed is printed whether supplied or generated, and so are the
    chosen filenames, because those are the account that survives a change of
    Python's sampling internals.

    Asking for more images than exist is an error rather than a clamp. A run
    silently reading seven when it was told ten looks exactly like a run that
    got what it asked for. Naming a file that is not there is an error for the
    same reason.
    """
    if options.screenshots is None:
        raise ExtractionError("no screenshot directory was given.")
    images = available_screenshots(options.screenshots)
    if not images:
        raise ExtractionError(f"no .png screenshots in {options.screenshots}")

    selection = options.selection
    if selection is None or (selection.explicit is None and selection.count is None):
        return images

    if selection.explicit is not None:
        by_name = {p.name: p for p in images}
        unknown = [n for n in selection.explicit if n not in by_name]
        if unknown:
            raise ExtractionError(
                f"not in {options.screenshots}: {', '.join(sorted(unknown))}"
            )
        if not selection.explicit:
            raise ExtractionError("no screenshots were selected.")
        chosen = tuple(sorted({by_name[n] for n in selection.explicit}))
        if progress is not None:
            progress.emit(
                "B1",
                f"reading {len(chosen)} of {len(images)} screenshot(s), chosen by name",
                files=[p.name for p in chosen],
            )
            for image in chosen:
                progress.emit("", image.name)
        return chosen

    count = selection.count
    assert count is not None
    if count < 1:
        raise ExtractionError(f"screenshot count must be at least 1, got {count}")
    if count > len(images):
        raise ExtractionError(
            f"screenshot count {count} exceeds the {len(images)} .png file(s) "
            f"in {options.screenshots}"
        )

    seed = selection.seed
    if seed is None:
        seed = random.randrange(2**32)
    chosen = tuple(sorted(random.Random(seed).sample(images, count)))
    if progress is not None:
        progress.emit(
            "B1",
            f"sampling {len(chosen)} of {len(images)} screenshot(s), seed {seed}",
            files=[p.name for p in chosen],
            seed=seed,
        )
        for image in chosen:
            progress.emit("", image.name)
    return chosen


def screenshots_for(
    options: RunOptions, progress: Progress | None = None
) -> tuple[Path, ...]:
    """Which images B1 will read, resolved before A1 rather than during B1.

    This used to happen inside `build_roster`, during planning, which was the
    natural place while the images were only B1's input. They are also the
    thing an `image` evaluation context is keyed on, and A1 is where agent
    configs are retrieved -- so an image resolved after A1 cannot influence
    what LaunchDarkly serves for the stage that reads it. Resolving here is
    what makes the `image` kind able to do anything at all.

    Two things fall out and both are improvements. A bad `--screenshot-count`
    now fails before the SDK client is constructed rather than after a run row
    has been appended, and the filenames are known in time to go on the run
    record at A1 instead of being attached later.

    A run with no screenshot directory resolves to nothing rather than
    raising: the roster file supplies the people, which is the ordinary path.
    """
    if options.screenshots is None:
        if options.selection is not None:
            raise ExtractionError(
                "a screenshot selection was given without a screenshot "
                "directory. Nothing would sample."
            )
        return ()
    return resolve_screenshots(options, progress)


def extraction_reasons(
    images: tuple[Path, ...], selection: ScreenshotSelection | None
) -> dict[str, Any]:
    """What to record on the run row about which images B1 read.

    The seed reconstructs a sample; nothing reconstructs an explicit choice,
    so the filenames go on the record either way. Without this, a run planned
    from a browser could not say afterwards what it had read.
    """
    reasons: dict[str, Any] = {"screenshots": [p.name for p in images]}
    # LaunchDarkly is given a content hash and never a filename, so this map
    # is the only thing that can say which file a served variation read. It
    # lives in the committed ledger, which is where design 2 requires a
    # surprising run to be diagnosable from.
    reasons["screenshot_keys"] = {
        image.name: image.key for image in map(ImageIdentity.of, images)
    }
    if selection is not None and selection.seed is not None:
        reasons["screenshot_seed"] = selection.seed
    return reasons


def extraction_model(options: RunOptions, images: tuple[Path, ...]) -> Any:
    """B1's vision model, or a recording of one.

    Replay exists for B1 because `tests/fixtures/b1-extractions.json` is a
    real capture and a demo that pays for seven vision calls to show a form
    submitting is a demo nobody runs twice. The images are passed in because
    the recording is matched on image content -- reading three of seven has to
    replay the right three.
    """
    if options.extractions is not None:
        return RecordedVision.from_file(options.extractions, images)
    return AnthropicModel()


def build_roster(
    options: RunOptions,
    run: Any = None,
    images: tuple[Path, ...] = (),
    progress: Progress | None = None,
) -> tuple[Any, Any]:
    """The run input, from the file and -- when asked -- from screenshots.

    The file always supplies the origin, the candidate ship dates and the lane
    declarations, because a screenshot says nothing about any of them. With a
    screenshot directory, B1 supplies the people and the file's `recipients`
    key becomes optional.

    Returns the roster and B1's `ExtractionResult`, or `None` for a run that
    read no screenshots. The result is returned rather than folded into the
    roster because `Roster` is the run *input*, and what B1 could not read is
    a fact about the reading. It is returned rather than only emitted because
    the progress stream that reports it has scrolled away by the time anyone
    reads the summary: a screenshot that produced nothing is the difference
    between a short roster and a broken run, and only one of those is worth
    acting on.

    `images` is resolved by `screenshots_for` before A1 and passed in, rather
    than chosen here: B1's config is retrieved at run start under a context
    keyed on the images, so this function cannot be the one that picks them.
    """
    progress = progress or NullProgress()
    if not images:
        roster = load_roster(
            options.recipients, lane_book=lane_book(options, progress)
        )
        return roster, None

    roster = load_roster(
        options.recipients,
        lane_book=lane_book(options, progress),
        require_recipients=False,
    )

    progress.emit("B1", f"reading {len(images)} screenshot(s)", count=len(images))
    extracted = extract_from_images(
        run, images, model=extraction_model(options, images), ledger_root=options.ledger
    )
    progress.emit("B1", extracted.describe(), summary=extracted.describe())
    for u in extracted.unresolved:
        progress.emit("", f"no address: {u.name} — {u.note[:70]}", unresolved=u.name)
    for u in extracted.unreadable:
        # The reason, not just the filename: "no JSON object in the reply" is
        # a console edit and "invalid JSON" is a model choice, and the
        # operator watching a run is the person who can tell them apart.
        progress.emit("", f"UNREADABLE: {u.name} — {u.reason}", unreadable=u.name)

    # Lanes are assigned from the file's book, the same as for a hand-written
    # roster -- extraction produces an address, not an ambient assumption.
    book = lane_book(options)
    recipients = extracted.recipients
    if book is not None and roster.ship_dates:
        season = roster.ship_dates[0]
        recipients = tuple(
            replace(r, lane=book.lane_for(r.address.state, season)) for r in recipients
        )
    return roster.with_recipients(recipients), extracted


def repair_model(options: RunOptions, planner: PlannerMode) -> Any:
    """B3's model, when `planner-mode` is not off and there are images."""
    if planner is PlannerMode.OFF or options.screenshots is None:
        return None
    if options.repairs is not None:
        return RecordedConversation.from_file(options.repairs)
    return AnthropicModel()


def quoter(options: RunOptions) -> Any:
    """Recorded quotes if asked for, live Shippo otherwise.

    There is no third option. A quoter that invents a rate when the API is
    unreachable would put a made-up cost on a manifest a human is about to
    approve, so both of these raise instead.
    """
    if options.quotes:
        return RecordedQuoter.from_file(options.quotes)
    options.cache.mkdir(parents=True, exist_ok=True)
    return ShippoQuoter(
        cache_path=options.cache / "shippo-quotes.json",
        max_attempts=options.max_attempts,
        backoff_s=options.backoff,
    )


def validator(options: RunOptions, mode: ValidationMode) -> Any:
    """None when `validation-mode` is off, so nothing connects needlessly."""
    if mode is ValidationMode.OFF:
        return None
    if options.validations:
        return RecordedAddressValidator.from_file(options.validations)
    options.cache.mkdir(parents=True, exist_ok=True)
    return ShippoAddressValidator(cache_path=options.cache / "shippo-addresses.json")


def verifier(options: RunOptions, mode: VerificationMode) -> Any:
    """None when `verification-enabled` is off, so nothing connects needlessly."""
    if mode is not VerificationMode.ON:
        return None
    if options.completions:
        return RecordedModel.from_file(options.completions)
    return AnthropicModel()


def conversing_model(options: RunOptions) -> Any:
    """D2's `review-narrator` model. Live-only, and this is the only seam.

    Every other agent has a recording to replay -- B1 `extractions`, B3
    `repairs`, D1 `completions` -- but the review conversation has none: a
    narration is open-ended and answers a human typing, so there is nothing to
    record it against. So an offline run has no narrator, and D2 degrades to the
    button-driven review, which is the same fallback the CLI takes when
    `review-narrator` is unavailable.

    Raising `ModelUnavailable` is how a front-end asks for that fallback rather
    than a special case: the service and `cli._review` both catch it. Routing
    the construction through here (rather than `AnthropicModel()` at the call
    site, as the CLI once did) keeps the module docstring's promise that this is
    the only place a socket-opening object is built -- so importing `service.py`
    in a test cannot open one.
    """
    if options.offline or not os.environ.get("ANTHROPIC_API_KEY"):
        raise ModelUnavailable(
            "review-narrator needs a live model and there is no recording to "
            "replay it from; this run is offline or has no ANTHROPIC_API_KEY, so "
            "the review is button-driven rather than conversational."
        )
    return AnthropicModel()


@dataclass
class RunContext:
    """A1's output plus how it was obtained, for a front-end to display."""

    run: Any
    connection: str
    snapshot: str
    client: Any = None
    #: Set once planning has run. Kept here so a front-end holds one object.
    roster: Any = None
    #: B1's `ExtractionResult`, or None on a run that read no screenshots. The
    #: roster says who was found; this says what could not be read, which a
    #: summary reporting only the first cannot distinguish from a short list.
    extraction: Any = None
    images: tuple[Path, ...] = ()
    result: Any = None
    extra_reasons: dict[str, Any] = field(default_factory=dict)


def open_run(options: RunOptions, progress: Progress | None = None) -> RunContext:
    """A1, wired to whatever LaunchDarkly actually returns.

    The client is returned open on purpose and the caller must close it. D1
    reports its metrics against the variation LaunchDarkly served, and that
    needs the same client -- closing after A1 would silently drop every AI
    Config metric. The SDK also runs a background thread, so a caller that
    forgets will hang.

    Screenshots are resolved first, before the client exists: they are part of
    what A1 evaluates against, and a bad selection should not cost a socket or
    leave a run row behind.
    """
    progress = progress or NullProgress()
    images = screenshots_for(options, progress)
    # Resolved here, before the client exists, for the same reason screenshots
    # are: an operator key that names nobody is an operator error, and it
    # should cost neither a socket nor a run row. This is also the only place
    # a key becomes a key/department pair, so the two front-ends cannot end up
    # with different ideas of who is in which department.
    operator = OperatorPool.load(options.operators).get(options.operator)

    client = None if options.offline else launchdarkly_client(
        timeout_seconds=options.timeout
    )
    try:
        connection, gate, agent_source = flag_sources(options, client)
        before = options.snapshot.read_bytes() if options.snapshot.exists() else None
        run = initialize_run(
            ledger_root=options.ledger,
            gate=gate,
            agent_source=agent_source,
            snapshot_path=options.snapshot,
            profile=options.profile,
            campaign=options.campaign,
            packet_count=options.packet_count,
            # B1's config is retrieved once per image, under a context keyed
            # on each image's content hash. This is why they are resolved
            # before A1 rather than during planning.
            images=tuple(map(ImageIdentity.of, images)),
            operator=operator,
        )
    except BaseException:
        if client is not None:
            client.close()
        raise
    return RunContext(
        run=run,
        connection=connection,
        snapshot=snapshot_state(options.snapshot, before),
        client=client,
        images=images,
        extra_reasons=extraction_reasons(images, options.selection) if images else {},
    )


def extract_with(
    context: RunContext, options: RunOptions, progress: Progress | None = None
) -> RunContext:
    """B1 against an already-opened run, and then stop.

    This is `run extract`'s body, moved here when the UI wanted the same
    depth. Two front-ends reaching the same seam by writing it out twice is
    how the CLI and the browser drift, and the `record_run_reasons` call below
    is the exact detail that would have been missed: `plan_with` records the
    reasons on its way through planning, so a run that stops before planning
    has to record them itself or every B1 invocation hash names a screenshot
    the ledger cannot resolve back to a filename.
    """
    progress = progress or NullProgress()
    if options.screenshots is None:
        raise ExtractionError(
            "an extract-only run needs screenshots; there is nothing for B1 "
            "to read, and the roster file needs no extracting."
        )
    context.roster, context.extraction = build_roster(
        options, context.run, context.images, progress
    )
    record_run_reasons(options.ledger, context.run, context.extra_reasons)
    progress.emit(
        "B1",
        f"{context.roster.packet_count} recipient(s) — stopping, extract only",
        packet_count=context.roster.packet_count,
    )
    return context


def run_with(
    context: RunContext, options: RunOptions, progress: Progress | None = None
) -> RunContext:
    """Whichever depth was asked for. The only thing that reads `depth`."""
    if options.depth is RunDepth.EXTRACT:
        return extract_with(context, options, progress)
    return plan_with(context, options, progress)


def plan_with(
    context: RunContext, options: RunOptions, progress: Progress | None = None
) -> RunContext:
    """B1 through D1 against an already-opened run.

    Separate from `open_run` because a front-end shows the capability header
    the moment A1 lands and then waits on planning, and a single call would
    make it wait for both.
    """
    from .plan import plan_run

    progress = progress or NullProgress()
    run = context.run
    # `images` and `extra_reasons` were settled by `open_run`, which is what
    # lets B1's config be retrieved under a context keyed on them.
    roster, extraction = build_roster(options, run, context.images, progress)
    context.roster = roster
    context.extraction = extraction

    mode = run.validation()
    verification = run.verification()
    progress.emit(
        "roster",
        f"{roster.source}  ({roster.packet_count} recipients)",
        packet_count=roster.packet_count,
    )
    progress.emit("ship dates", ", ".join(d.isoformat() for d in roster.ship_dates))
    progress.emit("validation", mode.value)
    progress.emit("verification", verification.value)
    progress.emit("quotes", str(options.quotes) if options.quotes else "live (Shippo)")

    context.result = plan_run(
        run,
        roster,
        ledger_root=options.ledger,
        quoter=quoter(options),
        validator=validator(options, mode),
        lane_book=lane_book(options),
        repairer=repair_model(options, run.planner()),
        screenshots=options.screenshots,
        verifier=verifier(options, verification),
        extra_reasons=context.extra_reasons,
    )
    return context
