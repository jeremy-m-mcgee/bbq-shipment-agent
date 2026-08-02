"""A1: initialize a run. Design section 4, Phase A.

Generate a run ID, read the kill switch from repo config and abort if set,
evaluate the flag payload against the run context, clamp authority against the
repo ceiling, and record the resolved capability set plus what the flag layer
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .agent_configs import (
    DEFAULT_SNAPSHOT_PATH,
    AgentConfig,
    AgentConfigSource,
    OfflineAgentConfigs,
    fetch_agent_configs,
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
from .context import STAGE_RUN_INIT, build_context, reason_code, to_ld_context
from .ledger import AgentInvocationRecord, LedgerWriter, RunRecord, rebuild, utc_now

#: LD flag key -> the capability it proposes. Design 6.1 and 6.8.
#:
#: `authority-level` is deliberately absent. Design 6.5 keeps authority in
#: repo config precisely because LD's core virtue, immediate and easy change,
#: is the wrong property for the flag governing whether the system can spend
#: money. The clamp in `resolve` runs regardless, as defense in depth against
#: a provider that proposes one anyway.
CAPABILITY_FLAGS: dict[str, str] = {
    "planner-mode": "planner",
    "memory-mode": "memory",
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
    returns still goes through the ceiling clamp and the prerequisites, so a
    misbehaving provider cannot expand what the run may do.
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

    Proposes only. Everything it returns still passes through the ceiling
    clamp and the prerequisites, and it is never asked for `authority-level`
    at all -- see the note on `CAPABILITY_FLAGS`.
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
    campaign: str | None = None
    packet_count: int | None = None
    #: Agent key -> the config retrieved at run start. Captured once so every
    #: invocation records the text the agent actually ran on, rather than
    #: whatever LD happens to serve when the record is written.
    agent_configs: dict[str, AgentConfig] = field(default_factory=dict)

    @property
    def capabilities(self):
        return self.resolved.capabilities

    @property
    def cap_fingerprint(self) -> str:
        return self.resolved.fingerprint

    def context_for_stage(
        self,
        stage: str,
        *,
        recipient_key: str | None = None,
        shipment_attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The multi-context for evaluating a flag at a later stage."""
        return build_context(
            run_id=self.run_id,
            stage=stage,
            profile=self.resolved.profile,
            campaign=self.campaign,
            packet_count=self.packet_count,
            recipient_key=recipient_key,
            shipment_attributes=shipment_attributes,
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

    context = build_context(
        run_id=run_id,
        stage=STAGE_RUN_INIT,
        profile=requested_profile,
        campaign=campaign,
        packet_count=packet_count,
    )

    payload = (provider or OfflineProvider()).fetch(context)
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
        campaign=campaign,
        packet_count=packet_count,
    )

    # Pulled after the run exists so each agent is evaluated under this run's
    # identity and its own `stage` kind. Evaluating all four under one context
    # would make the per-agent targeting of 6.6 silently ineffective.
    run.agent_configs = fetch_agent_configs(
        agent_source or OfflineAgentConfigs(), run.context_for_stage
    )

    # Design 6.4 mitigation 1, the highest-value one: an instruction naming a
    # tool Python does not offer becomes a startup error rather than a
    # mid-run surprise. Deliberately before the snapshot write and the ledger
    # append -- a run that cannot legally proceed should leave neither a
    # cached config nor a half-open row behind.
    assert_tool_contract(run.agent_configs)

    # Design 6.4 mitigation 3, and the offline cache of 6.10 in one artifact.
    # Skipped when nothing usable came back: `write_snapshot` already refuses
    # to overwrite good cached instructions with nothing, and an
    # all-unavailable run has nothing to add, so writing would only create an
    # empty file on the very path that needs the cache intact.
    if any(config.available for config in run.agent_configs.values()):
        write_snapshot(run.agent_configs, snapshot_path)

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


def record_agent_invocation(
    ledger_root: Path | str,
    run: Run,
    agent_key: str,
    *,
    outcome: str,
    iterations: int = 1,
    shipment_key: str | None = None,
) -> AgentInvocationRecord:
    """Append the ledger record for one agent invocation.

    Identity comes off the config captured at A1, never from a fresh lookup,
    so the record describes the text the agent actually ran on. Re-reading LD
    here would attribute a behavior to whatever the console happens to be
    serving at write time, which is the exact confusion the instruction hash
    exists to prevent.

    Invocations have no merge key: two calls to the same agent on the same
    shipment are two facts, not a correction of one another.
    """
    config = run.agent_configs.get(agent_key)
    if config is None:
        raise KeyError(
            f"{agent_key}: no config was retrieved at run start. "
            f"Retrieved: {sorted(run.agent_configs) or 'nothing'}."
        )

    record = AgentInvocationRecord(
        run_id=run.run_id,
        agent_key=agent_key,
        shipment_key=shipment_key,
        instruction_variation_key=config.variation_key,
        instruction_version=config.version,
        instruction_hash=config.instruction_hash,
        model=config.model,
        iterations=iterations,
        outcome=outcome,
    )
    LedgerWriter(ledger_root).append(record)
    return record
