"""A1: initialize a run. Design section 4, Phase A.

Generate a run ID, read the kill switch from repo config and abort if set,
evaluate the flag payload against the run context, apply the repo's declared
prerequisites, and record the resolved capability set plus what the flag layer
proposed on the run record.

The flag layer sits behind `CapabilityProvider`. `OfflineProvider` is not a
degraded stand-in for the real thing -- section 6.10 makes unreachable a normal
path, because this is a CLI that runs three to five times a year and hits cold
start every time. A run with no instructions available falls back to the
deterministic spine and a manually reviewed manifest: less helpful, not less
correct.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .agent_configs import (
    DEFAULT_SNAPSHOT_PATH,
    EXTRACTION_KEY,
    AgentConfig,
    AgentConfigSource,
    OfflineAgentConfigs,
    archived_variations,
    fetch_agent_configs,
    fetch_extraction_configs,
    write_snapshot,
)
from .capabilities import (
    DEFAULT_CONFIG_PATH,
    CapabilityConfig,
    KillSwitchEngaged,
    ResolvedCapabilities,
    resolve,
)
from .agents.tools import assert_tool_contract
from .context import (
    STAGE_RUN_INIT,
    ContextBuilder,
    ImageIdentity,
    reason_code,
    to_ld_context,
)
from .ledger import AgentInvocationRecord, LedgerWriter, RunRecord, rebuild, utc_now

#: LD flag key -> the capability it proposes. Design 6.1 and 6.8.
#:
#: `authority-level` and `memory-mode` were here and are gone with the
#: capabilities they proposed -- nothing read either one. A flag key that
#: resolves to no field would still be evaluated, still cost a round trip, and
#: still write a reason to the ledger for a value no stage consults, which is
#: the decorative-permission failure design 6.5 warned about.
CAPABILITY_FLAGS: dict[str, str] = {
    "planner-mode": "planner",
    "validation-mode": "validation",
    "verification-enabled": "verification",
}


@dataclass(frozen=True)
class FlagPayload:
    """What the flag layer proposed, and where it came from."""

    overrides: dict[str, str] = field(default_factory=dict)
    #: "launchdarkly" | "cache" | "unavailable"
    source: str = "unavailable"
    reason: str = "OFFLINE"


class CapabilityProvider(Protocol):
    """Source of proposed capability values.

    Deliberately narrow: a provider proposes, it does not decide. Whatever it
    returns still goes through the repo's prerequisites, and a key the repo has
    no field for is recorded and ignored, so a misbehaving provider cannot
    change what the run does beyond the three capabilities a stage reads.
    """

    def fetch(self, context: dict[str, Any]) -> FlagPayload: ...


class OfflineProvider:
    """Proposes nothing, so the run operates on the config profile alone.

    Used when there is no SDK key and no cached payload. Section 6.10 step 4:
    fall back to `baseline`, which degrades cleanly by construction.
    """

    def __init__(self, reason: str = "NO_SDK_KEY") -> None:
        self._reason = reason

    def fetch(self, context: dict[str, Any]) -> FlagPayload:
        return FlagPayload(source="unavailable", reason=self._reason)


class LaunchDarklyProvider:
    """Evaluates the capability flags in `CAPABILITY_FLAGS`.

    Proposes only. Everything it returns still passes through the repo's
    prerequisites, and a key naming a capability the repo does not have is
    recorded and ignored rather than acted on -- see `resolve`.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch(self, context: dict[str, Any]) -> FlagPayload:
        ld_context = to_ld_context(context)
        overrides: dict[str, str] = {}
        reasons: list[str] = []
        for flag_key, capability in CAPABILITY_FLAGS.items():
            detail = self._client.variation_detail(flag_key, ld_context, None)
            reasons.append(f"{flag_key}:{reason_code(detail.reason)}")
            if detail.value is None:
                # No such flag, or it served null. Saying nothing leaves the
                # profile's value standing, which is the right default: an
                # absent flag is not an instruction to change anything.
                continue
            overrides[capability] = str(detail.value)
        return FlagPayload(
            overrides=overrides,
            source="launchdarkly",
            reason=" ".join(reasons) if reasons else "NO_FLAGS",
        )


def launchdarkly_client(
    *, sdk_key: str | None = None, timeout_seconds: float = 5.0
) -> Any | None:
    """Bring up the SDK, or return None if it is not usable.

    Falsy rather than `is None` on the key by necessity: `UV_ENV_FILE` makes
    `uv run` load `.env`, and setup.sh seeds `.env` from `.env.example`, so an
    unconfigured key is present-but-empty rather than absent. Handing `""` to
    the SDK produces a client that never initializes and a run that blocks for
    the whole timeout before failing.

    A client that does not come up inside the timeout is reported unavailable
    rather than raised. Section 6.10 makes unreachable a normal path: this is
    a CLI that hits cold start every time it runs, and the caller's fallback
    is `baseline` plus the cached snapshot, which degrades cleanly.
    """
    key = os.environ.get("LD_SDK_KEY", "") if sdk_key is None else sdk_key
    if not key:
        return None

    from ldclient import Config, LDClient

    client = LDClient(Config(key), start_wait=timeout_seconds)
    if not client.is_initialized():
        client.close()
        return None
    return client


@dataclass
class Run:
    """A live run: its identity, its capabilities, and how it got them."""

    run_id: str
    resolved: ResolvedCapabilities
    payload: FlagPayload
    started_at: str
    #: The one context builder for this run, carried rather than rebuilt.
    #: A1 constructs it before the provider is contacted, so the context the
    #: capability flags were evaluated against is the same object every later
    #: stage retrieves under -- see the module docstring in `context`.
    contexts: ContextBuilder
    campaign: str | None = None
    packet_count: int | None = None
    #: Agent key -> the config retrieved at run start. Captured once so every
    #: invocation records the text the agent actually ran on, rather than
    #: whatever LD happens to serve when the record is written.
    agent_configs: dict[str, AgentConfig] = field(default_factory=dict)
    #: Image content hash -> B1's config for *that* image. Empty on a roster
    #: run, where B1 does not run at all. The one place a stage's config is
    #: held per unit rather than per run -- see `fetch_extraction_configs`.
    image_configs: dict[str, AgentConfig] = field(default_factory=dict)

    @property
    def capabilities(self):
        return self.resolved.capabilities

    @property
    def cap_fingerprint(self) -> str:
        return self.resolved.fingerprint

    def context_for_stage(self, stage: str) -> dict[str, Any]:
        """The multi-context for evaluating something at a later stage."""
        return self.contexts.for_stage(stage)

    def config_for_image(self, image: ImageIdentity) -> AgentConfig | None:
        """B1's config for one screenshot, as retrieved at run start.

        Falls back to the run-scoped config when this image was not among the
        ones A1 resolved -- which happens when B1 is called directly rather
        than through the front-ends. Identity still comes from A1 either way;
        this never looks anything up.
        """
        return self.image_configs.get(image.key) or self.agent_configs.get(
            EXTRACTION_KEY
        )

    def evaluation_reasons(self) -> dict[str, Any]:
        """Everything needed to explain this run's capabilities later."""
        return {
            "capabilities": dict(self.resolved.reasons),
            "flag_payload_source": self.payload.source,
            "flag_payload_reason": self.payload.reason,
        }


def count_shadow_runs(ledger_root: Path | str) -> int:
    """Completed runs that actually operated with the planner in shadow.

    Counted from the recorded capability snapshot rather than from `profile`,
    because profile definitions are edited over time while the ledger is
    append-only. A run is only counted once it completed -- a shadow run that
    crashed halfway has not demonstrated anything.
    """
    connection = rebuild(ledger_root)
    try:
        # json_extract_string, not the `->>` operator: DuckDB mis-resolves the
        # `->>` overload inside a compound predicate and tries to read the
        # path as a numeric index. The named function is unambiguous.
        (count,) = connection.execute(
            "SELECT count(*) FROM runs "
            "WHERE completed_at IS NOT NULL "
            "AND json_extract_string(cap_snapshot, '$.capabilities.planner') = 'shadow'"
        ).fetchone()
        return int(count)
    finally:
        connection.close()


def initialize_run(
    *,
    ledger_root: Path | str,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    provider: CapabilityProvider | None = None,
    agent_source: AgentConfigSource | None = None,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    profile: str | None = None,
    campaign: str | None = None,
    packet_count: int | None = None,
    run_id: str | None = None,
    images: tuple[ImageIdentity, ...] = (),
) -> Run:
    """A1. Returns the run record's in-memory counterpart.

    Ordering is load-bearing twice over. The kill switch is read before
    anything else happens and before any provider is contacted, so an operator
    disabling the pipeline is not racing a network call. And the run record is
    appended last, so a failure anywhere in retrieval leaves no half-open run
    on disk -- the ledger is append-only, and a row written in error cannot be
    taken back.

    Both sources default to offline. Live retrieval is injected rather than
    reached for, so no test and no `--help` invocation can open a socket.
    """
    config = CapabilityConfig.load(config_path)
    if config.kill_switch:
        raise KillSwitchEngaged(
            f"{config_path}: kill_switch is true. Set it to false to run."
        )

    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    requested_profile = profile or config.default_profile

    # Built before the provider is contacted, so A1's own evaluation and every
    # later stage's retrieval go through one object rather than two dicts that
    # agree by coincidence. `requested_profile` is what `resolve` will report
    # back as `resolved.profile` -- both are `profile or default_profile` --
    # so carrying it forward is not a guess about the outcome.
    # `image_count` is derived from `images` rather than passed alongside it,
    # so the number a targeting rule sees and the set B1's configs are
    # retrieved for cannot disagree. Zero on a roster run, which is a fact
    # about that run and not a missing value.
    contexts = ContextBuilder(
        run_id=run_id,
        profile=requested_profile,
        campaign=campaign,
        packet_count=packet_count,
        image_count=len(images),
    )

    payload = (provider or OfflineProvider()).fetch(contexts.for_stage(STAGE_RUN_INIT))
    resolved = resolve(
        config,
        profile=requested_profile,
        overrides=payload.overrides,
        shadow_runs=count_shadow_runs(ledger_root),
    )

    run = Run(
        run_id=run_id,
        resolved=resolved,
        payload=payload,
        started_at=utc_now(),
        contexts=contexts,
        campaign=campaign,
        packet_count=packet_count,
    )

    # Pulled after the run exists so each agent is evaluated under this run's
    # identity and its own `stage` kind. Evaluating all four under one context
    # would make the per-agent targeting of 6.6 silently ineffective.
    source = agent_source or OfflineAgentConfigs()
    run.agent_configs = fetch_agent_configs(source, run.context_for_stage)

    # B1 again, once per screenshot. The run-scoped fetch above stays: it is
    # what the snapshot commits, what the offline path reads back, and the
    # answer for a run with no screenshots at all.
    run.image_configs = fetch_extraction_configs(source, contexts, images)
    archived = archived_variations(run.agent_configs, run.image_configs)

    # Design 6.4 mitigation 1, the highest-value one: an instruction naming a
    # tool Python does not offer becomes a startup error rather than a
    # mid-run surprise. Deliberately before the snapshot write and the ledger
    # append -- a run that cannot legally proceed should leave neither a
    # cached config nor a half-open row behind.
    # Every served variation, not just the run-scoped one: a rollout can put a
    # second `screenshot-extraction` instruction into this run, and it gets the
    # same startup check as the first.
    assert_tool_contract({**run.agent_configs, **archived})

    # Design 6.4 mitigation 3, and the offline cache of 6.10 in one artifact.
    # Skipped when nothing usable came back: `write_snapshot` already refuses
    # to overwrite good cached instructions with nothing, and an
    # all-unavailable run has nothing to add, so writing would only create an
    # empty file on the very path that needs the cache intact.
    if any(config.available for config in run.agent_configs.values()):
        write_snapshot({**run.agent_configs, **archived}, snapshot_path)

    LedgerWriter(ledger_root).append(
        RunRecord(
            run_id=run.run_id,
            profile=resolved.profile,
            cap_fingerprint=run.cap_fingerprint,
            cap_snapshot=resolved.to_snapshot(),
            packet_count=packet_count,
            flag_payload=dict(payload.overrides),
            evaluation_reasons=run.evaluation_reasons(),
            started_at=run.started_at,
        )
    )
    return run


def record_run_reasons(
    ledger_root: Path | str,
    run: Run,
    extra_reasons: dict[str, Any] | None = None,
) -> RunRecord:
    """Append what a run learned about itself to the row A1 opened.

    For a run that stops before planning -- `run extract` is the only one
    today -- this is the whole of design 2's reconstructibility requirement.
    Which screenshots B1 read, and which content hash each one has, exist
    nowhere else: LaunchDarkly is given the hash and never the filename, so
    without this append the `image_key` on every invocation record points at
    a file nothing in the committed JSONL can name.

    **Merge from `run.evaluation_reasons()`, never from nothing.**
    `evaluation_reasons` is one JSON field, so an append rewrites it whole and
    a writer that starts from a bare dict silently drops what A1 recorded.
    `plan._record_planning` merges the same way for the same reason; this
    function exists so the two front-ends cannot disagree about it.
    """
    reasons = run.evaluation_reasons()
    reasons.update(extra_reasons or {})
    record = RunRecord(run_id=run.run_id, evaluation_reasons=reasons)
    LedgerWriter(ledger_root).append(record)
    return record


def record_agent_invocation(
    ledger_root: Path | str,
    run: Run,
    agent_key: str,
    *,
    outcome: str,
    iterations: int = 1,
    shipment_key: str | None = None,
    image_key: str | None = None,
    tools_offered: Sequence[str] | None = None,
    tools_called: Sequence[str] | None = None,
    config: AgentConfig | None = None,
) -> AgentInvocationRecord:
    """Append the ledger record for one agent invocation.

    Identity comes off the config captured at A1, never from a fresh lookup,
    so the record describes the text the agent actually ran on. Re-reading LD
    here would attribute a behavior to whatever the console happens to be
    serving at write time, which is the exact confusion the instruction hash
    exists to prevent.

    Invocations have no merge key: two calls to the same agent on the same
    shipment are two facts, not a correction of one another.

    `config` is passed when the caller holds one the run-scoped map does not:
    B1's config is retrieved per image, so the run-scoped entry would name the
    wrong variation on any run where a rollout served more than one. Passing
    it is still identity from A1 -- it is the config that image was served at
    run start, not a lookup.

    `tools_offered` and `tools_called` come from the loop that ran, not from
    `TOOL_NAMES`: the registry is what Python *can* offer, and what an agent
    was actually handed depends on the run (no validator, no screenshots, no
    tools). A caller with no tool loop passes neither and the columns stay
    absent, which is not the same fact as an empty list -- see the schema.
    """
    config = config or run.agent_configs.get(agent_key)
    if config is None:
        raise KeyError(
            f"{agent_key}: no config was retrieved at run start. "
            f"Retrieved: {sorted(run.agent_configs) or 'nothing'}."
        )

    record = AgentInvocationRecord(
        run_id=run.run_id,
        agent_key=agent_key,
        shipment_key=shipment_key,
        image_key=image_key,
        instruction_variation_key=config.variation_key,
        instruction_version=config.version,
        instruction_hash=config.instruction_hash,
        model=config.model,
        iterations=iterations,
        outcome=outcome,
        tools_offered=None if tools_offered is None else list(tools_offered),
        tools_called=None if tools_called is None else list(tools_called),
    )
    LedgerWriter(ledger_root).append(record)
    return record
