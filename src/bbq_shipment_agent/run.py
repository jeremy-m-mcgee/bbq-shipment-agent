"""A1: initialize a run. Design section 4, Phase A.

Generate a run ID, evaluate the kill switch against LaunchDarkly and abort if
it is on, and open the run record. Capabilities are *not* resolved here: since
the LaunchDarkly refactor each one is evaluated live, under its own stage
context, at the point the stage that reads it runs (design 6). A1's job is the
run's identity and the kill switch, nothing more.

The live flag seam is `FlagGate`. `OfflineGate` is not a degraded stand-in --
section 6.10 makes unreachable a normal path, because this is a CLI that runs
three to five times a year and hits cold start every time. A run with no LD
connection falls back to the per-capability defaults (planner off, validation
standard, verification off) and a manually reviewed manifest: less helpful, not
less correct.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from .agents.tools import assert_tool_contract
from .capabilities import (
    CAPABILITIES,
    DEFAULT_PROFILE,
    KILL_SWITCH_FLAG,
    CapabilitySet,
    FlagEvaluation,
    FlagGate,
    KillSwitchEngaged,
    OfflineGate,
    PlannerMode,
    ValidationMode,
    VerificationMode,
    evaluate_capability,
    evaluate_kill_switch,
)
from .context import (
    STAGE_RUN_INIT,
    ContextBuilder,
    ImageIdentity,
)
from .ledger import (
    AgentInvocationRecord,
    CapabilityEvaluationRecord,
    LedgerWriter,
    RunRecord,
    utc_now,
)
from .operators import Operator


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
    is the offline gate plus the cached snapshot, which degrades cleanly.
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
    """A live run: its identity, and the gate it evaluates capabilities through.

    Capabilities are evaluated lazily and cached. The first time a stage asks
    for one, it is evaluated live under that stage's context, the coerced value
    is cached, and the evaluation is appended to the `capability_evaluations`
    stream. Every later ask returns the cached value, so a run folds one value
    per capability into one snapshot and one fingerprint however many stages
    read it.

    A test may seed `capabilities` with a fixed set. A seeded value is already
    in the cache, so it is returned without contacting the gate or writing an
    evaluation record -- which is what lets a test plan against a fixed
    capability set without going near LaunchDarkly.
    """

    run_id: str
    #: The one context builder for this run, carried rather than rebuilt, so
    #: the context the kill switch was evaluated against and the one every
    #: later stage retrieves under are the same object -- see `context`.
    contexts: ContextBuilder
    #: The live flag seam. `OfflineGate` when LD is unreachable.
    gate: FlagGate
    #: Where capability evaluations are recorded. The ledger is the account of
    #: what a run operated under, and a live per-stage value exists nowhere
    #: else once the process exits.
    ledger_root: Path | str
    started_at: str
    #: The run's targeting label. No longer a bundle of values -- LD serves
    #: those -- but still recorded, because who a run claims to be changes what
    #: LD serves it.
    profile: str = DEFAULT_PROFILE
    #: The kill switch evaluation from A1, kept for the run row's reasons.
    kill_switch: FlagEvaluation | None = None
    campaign: str | None = None
    packet_count: int | None = None
    #: Agent key -> the config retrieved at run start. Captured once so every
    #: invocation records the text the agent actually ran on.
    agent_configs: dict[str, AgentConfig] = field(default_factory=dict)
    #: Image content hash -> B1's config for *that* image. Empty on a roster
    #: run, where B1 does not run at all.
    image_configs: dict[str, AgentConfig] = field(default_factory=dict)
    #: Capability name -> evaluated value / source / reason. Populated live as
    #: stages ask. Seeded runs pre-fill `_capabilities` only.
    _capabilities: dict[str, Any] = field(default_factory=dict)
    _cap_sources: dict[str, str] = field(default_factory=dict)
    _cap_reasons: dict[str, str] = field(default_factory=dict)

    def _evaluate(self, name: str) -> Any:
        """Evaluate one capability live, cache it, and record the evaluation.

        Cache-first: a value already known -- because a stage asked earlier, or
        a test seeded it -- is returned without touching the gate or the
        ledger. That keeps one value per capability per run and stops a re-read
        writing a duplicate event.

        """
        if name in self._capabilities:
            return self._capabilities[name]
        cap = CAPABILITIES[name]
        context = self.contexts.for_stage(cap.stage)
        value, source, reason = evaluate_capability(cap, self.gate, context)
        self._capabilities[name] = value
        self._cap_sources[name] = source
        self._cap_reasons[name] = reason
        LedgerWriter(self.ledger_root).append(
            CapabilityEvaluationRecord(
                run_id=self.run_id,
                stage=cap.stage,
                flag=cap.flag_key,
                value=value.value,
                source=source,
                reason=reason,
            )
        )
        return value

    def planner(self) -> PlannerMode:
        """B3's gate, evaluated under `stage: address_repair`."""
        return self._evaluate("planner")

    def validation(self) -> ValidationMode:
        """B2's mode, evaluated under `stage: address_validation`."""
        return self._evaluate("validation")

    def verification(self) -> VerificationMode:
        """D1's gate, evaluated under `stage: manifest_verification`."""
        return self._evaluate("verification")


    def resolved_capabilities(self) -> CapabilitySet | None:
        """The full set, once every capability has been evaluated.

        None until then: a fingerprint over a partial set would name an
        equivalence class no run belongs to. An extract-depth run, which reaches
        no capability-gated stage, keeps it None and writes no fingerprint.
        """
        if not all(name in self._capabilities for name in CAPABILITIES):
            return None
        return CapabilitySet(**{name: self._capabilities[name] for name in CAPABILITIES})

    @property
    def cap_fingerprint(self) -> str | None:
        resolved = self.resolved_capabilities()
        return resolved.fingerprint() if resolved is not None else None

    def cap_snapshot(self) -> dict[str, Any] | None:
        """Ledger-safe capability snapshot, or None if not fully evaluated.

        Resolved values and reasons only -- never a raw flag payload, because
        this is written to a committed, append-only file. The values are enum
        members' strings, structurally incapable of carrying free text.
        """
        resolved = self.resolved_capabilities()
        if resolved is None:
            return None
        return {
            "profile": self.profile,
            "capabilities": resolved.to_mapping(),
            "reasons": dict(self._cap_reasons),
        }

    def cap_reasons(self) -> dict[str, str]:
        return dict(self._cap_reasons)

    def cap_sources(self) -> dict[str, str]:
        return dict(self._cap_sources)

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
        """Everything needed to explain this run's capabilities later.

        `capabilities` grows as stages evaluate, so this reflects whatever has
        run when it is called -- the planning append carries the full set.
        `kill_switch` is A1's, and says whether LD was even reachable at run
        start, which is the one capability-adjacent fact settled before any
        stage runs.
        """
        reasons: dict[str, Any] = {"capabilities": dict(self._cap_reasons)}
        if self.kill_switch is not None:
            reasons["kill_switch"] = {
                "value": self.kill_switch.value,
                "source": self.kill_switch.source,
                "reason": self.kill_switch.reason,
            }
        if self.contexts.operator:
            # Recorded because a rule can now serve this run differently for
            # who it claimed to be, and design 2 wants a surprising run
            # diagnosable from the committed JSONL alone.
            reasons["operator"] = {
                "key": self.contexts.operator,
                "department": self.contexts.department,
            }
        return reasons


def initialize_run(
    *,
    ledger_root: Path | str,
    gate: FlagGate | None = None,
    agent_source: AgentConfigSource | None = None,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    profile: str | None = None,
    campaign: str | None = None,
    packet_count: int | None = None,
    run_id: str | None = None,
    images: tuple[ImageIdentity, ...] = (),
    operator: Operator | None = None,
) -> Run:
    """A1. Returns the run record's in-memory counterpart.

    Ordering is load-bearing twice over. The kill switch is evaluated before
    anything is fetched or written, so an operator disabling the pipeline is
    not racing an agent-config retrieval. And the run record is appended last,
    so a failure anywhere in retrieval leaves no half-open run on disk -- the
    ledger is append-only, and a row written in error cannot be taken back.

    Both the gate and the agent source default to offline. Live retrieval is
    injected rather than reached for, so no test and no `--help` invocation can
    open a socket.
    """
    gate = gate if gate is not None else OfflineGate()
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    requested_profile = profile or DEFAULT_PROFILE

    # Built before the kill switch is evaluated, so A1's evaluation and every
    # later stage's retrieval go through one object. `image_count` is derived
    # from `images` so the number a targeting rule sees and the set B1's
    # configs are retrieved for cannot disagree.
    contexts = ContextBuilder(
        run_id=run_id,
        profile=requested_profile,
        campaign=campaign,
        packet_count=packet_count,
        image_count=len(images),
        operator=operator.key if operator else None,
        department=operator.department if operator else None,
    )

    # The kill switch, live, under `stage: run_init`, before any fetch or
    # append. On when LD is reachable and a rule serves it true; off by default
    # when LD is unreachable, so an outage does not brick an offline run.
    kill = evaluate_kill_switch(gate, contexts.for_stage(STAGE_RUN_INIT))
    if kill.value:
        raise KillSwitchEngaged(
            f"{KILL_SWITCH_FLAG} is on in LaunchDarkly "
            f"({kill.source}:{kill.reason}). Turn it off to run."
        )

    run = Run(
        run_id=run_id,
        contexts=contexts,
        gate=gate,
        ledger_root=ledger_root,
        started_at=utc_now(),
        profile=requested_profile,
        kill_switch=kill,
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

    # Design 6.4 mitigation 1: an instruction naming a tool Python does not
    # offer becomes a startup error rather than a mid-run surprise. Before the
    # snapshot write and the ledger append -- a run that cannot legally proceed
    # should leave neither a cached config nor a half-open row behind.
    assert_tool_contract({**run.agent_configs, **archived})

    # Design 6.4 mitigation 3, and the offline cache of 6.10 in one artifact.
    if any(config.available for config in run.agent_configs.values()):
        write_snapshot({**run.agent_configs, **archived}, snapshot_path)

    # The run row opens without a capability snapshot: capabilities have not
    # been evaluated yet. Planning appends `cap_fingerprint`, `cap_snapshot`
    # and `flag_payload` once the stages have run -- a partial update per
    # design 7, the same shape as `completed_at` arriving at approval.
    LedgerWriter(ledger_root).append(
        RunRecord(
            run_id=run.run_id,
            profile=requested_profile,
            packet_count=packet_count,
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
    judge_score: float | None = None,
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
    wrong variation on any run where a rollout served more than one.

    `tools_offered` and `tools_called` come from the loop that ran, not from
    `TOOL_NAMES`: what an agent was actually handed depends on the run. A caller
    with no tool loop passes neither and the columns stay absent.

    `judge_score` is a judge's own line, not a grade attached to the thing it
    graded: the judge is an invocation and the score is what it produced.
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
        judge_score=judge_score,
    )
    LedgerWriter(ledger_root).append(record)
    return record
