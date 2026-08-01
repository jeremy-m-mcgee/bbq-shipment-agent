"""LaunchDarkly AI Configs: retrieval, the instruction hash, and the snapshot.

Design sections 6.2 (agent registry), 6.4 (instruction drift), 6.10 (offline).

Under the medium split LD holds the instruction text for the four agents, which
means the text that produced a given behavior is no longer recoverable from
`git log`. Three things here exist to buy that back: the instruction hash on
every invocation record, the run-start snapshot committed to the repo, and the
fact that the snapshot doubles as the offline instruction cache.

## Templates, not rendered text

Everything here stores and hashes the *un-rendered* instruction template.

That is a correctness requirement, not a preference. The AI SDK interpolates
Mustache variables into instructions and always injects the full evaluation
context as `ldctx`, so rendered instructions for `address-repair` would contain
the recipient key and shipment attributes. The snapshot is committed and the
ledger is committed and append-only, so recipient data written into either
cannot be removed by a later append.

Hashing the template is also the only thing that answers the question the hash
exists for. A rendered hash differs on every run by construction -- packet
count alone guarantees it -- so it flags a difference every time and
discriminates nothing. A template hash changes if and only if the text was
edited or targeting served a different variation.

The interpolated text is still what gets handed to the model at invocation
time; it is simply never what gets stored.

## Reading the raw variation rather than the typed helper

`LDAIClient.agent_config_template()` returns un-rendered instructions but drops
the variation key, which `AgentInvocationRecord.instruction_variation_key`
needs. The SDK gets that key by reading `_ldMeta.variationKey` off the raw flag
value (`ldai/client.py`), so this module reads the raw value once and parses it
in `_from_variation` -- one function to fix if the envelope ever changes.

Reading raw has a second benefit: the value is un-rendered by construction, so
there is no way to accidentally snapshot interpolated text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .context import (
    STAGE_ADDRESS_REPAIR,
    STAGE_INFEASIBILITY_REMEDIATION,
    STAGE_MANIFEST_VERIFICATION,
    STAGE_REVIEW_NARRATOR,
    reason_code,
)
from .hashing import short_hash

DEFAULT_SNAPSHOT_PATH = Path("config/ld-snapshot.json")

#: Bump when a stored field changes meaning. A snapshot is only ever read back
#: as a cache, so an unreadable one degrades to the offline path rather than
#: failing a run.
SNAPSHOT_SCHEMA_VERSION = 1

#: The four agents of design 6.2, each evaluated under its own `stage` context
#: so one flag can target them independently. Written out rather than derived
#: from the agent key by string substitution: the mapping between an LD config
#: key and a stage identifier is a fact worth being able to read.
AGENT_STAGES: dict[str, str] = {
    "address-repair": STAGE_ADDRESS_REPAIR,
    "infeasibility-remediation": STAGE_INFEASIBILITY_REMEDIATION,
    "manifest-verification": STAGE_MANIFEST_VERIFICATION,
    "review-narrator": STAGE_REVIEW_NARRATOR,
}

AGENT_KEYS: tuple[str, ...] = tuple(AGENT_STAGES)

#: LD's envelope key on every AI Config value. Internal to the AI Config
#: format rather than to any one SDK version, but confined to `_from_variation`
#: regardless.
_LD_META = "_ldMeta"


class AgentConfigError(Exception):
    """A stored agent config is not what it claims to be."""


@dataclass(frozen=True)
class AgentConfig:
    """One agent's resolved configuration, as retrieved or as cached.

    `instructions` is always the un-rendered template. `instruction_hash` is
    derived from it rather than stored alongside it, so the two can never
    disagree about which text this record describes.
    """

    agent_key: str
    enabled: bool = False
    variation_key: str | None = None
    version: int | None = None
    model: str | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    #: Un-rendered Mustache template. Never interpolated text.
    instructions: str | None = None
    #: Tool names the config *declares* it expects. A declaration to assert
    #: against Python's registry in build order step 6, never a source of
    #: executable tool definitions -- LD decides what an agent is told, Python
    #: decides what it can do (design 6.1).
    declared_tools: tuple[str, ...] = ()
    #: "launchdarkly" | "cache" | "unavailable"
    source: str = "unavailable"
    reason: str = "OFFLINE"

    @property
    def instruction_hash(self) -> str | None:
        """Hash of the instruction template, or None if there is no text."""
        if self.instructions is None:
            return None
        return short_hash(self.instructions)

    @property
    def available(self) -> bool:
        """Whether this config can actually drive an agent.

        Enabled but text-less is not available. An agent turned on in the
        console with no instructions is a misconfiguration, and running it on
        an empty prompt would be worse than not running it.
        """
        return self.enabled and bool(self.instructions)

    def to_snapshot(self) -> dict[str, Any]:
        """The committed form. Sorted and stable so diffs mean something.

        `source` and `reason` are deliberately absent: they describe how *this
        run* obtained the config, which is a per-run fact belonging in the
        ledger. Putting them here would make the file churn on every run and
        drown the signal it exists to carry.
        """
        return {
            "enabled": self.enabled,
            "variation_key": self.variation_key,
            "version": self.version,
            "model": self.model,
            "model_parameters": self.model_parameters,
            "instructions": self.instructions,
            "instruction_hash": self.instruction_hash,
            "declared_tools": list(self.declared_tools),
        }

    @classmethod
    def from_snapshot(cls, agent_key: str, data: dict[str, Any]) -> AgentConfig:
        instructions = data.get("instructions")
        config = cls(
            agent_key=agent_key,
            enabled=bool(data.get("enabled", False)),
            variation_key=data.get("variation_key"),
            version=data.get("version"),
            model=data.get("model"),
            model_parameters=data.get("model_parameters") or {},
            instructions=instructions,
            declared_tools=tuple(data.get("declared_tools") or ()),
            source="cache",
            reason="SNAPSHOT",
        )
        stored = data.get("instruction_hash")
        if stored is not None and stored != config.instruction_hash:
            # The file is committed, so a mismatch means it was hand-edited and
            # the audit trail no longer describes the text beside it. Loud is
            # right here: this is a repo problem, not a network problem, and it
            # is the same posture `CapabilityConfig.load` takes.
            raise AgentConfigError(
                f"{agent_key}: snapshot instruction_hash is {stored!r} but the "
                f"instructions beside it hash to {config.instruction_hash!r}. "
                "The snapshot was edited by hand; re-pull it from LaunchDarkly."
            )
        return config


def _from_variation(
    agent_key: str, variation: Any, *, source: str, reason: str
) -> AgentConfig:
    """Parse LD's raw AI Config value.

    The only place that knows the wire format. A non-dict value means the flag
    is missing or serving something that is not an AI Config, which is
    unavailable rather than an error -- section 6.10 makes a missing config a
    normal path that falls back, and the fallback is recorded.
    """
    if not isinstance(variation, dict):
        return AgentConfig(agent_key=agent_key, source="unavailable", reason=reason)

    meta = variation.get(_LD_META)
    meta = meta if isinstance(meta, dict) else {}

    model_raw = variation.get("model")
    model_raw = model_raw if isinstance(model_raw, dict) else {}

    tools_raw = variation.get("tools")
    declared_tools = tuple(sorted(tools_raw)) if isinstance(tools_raw, dict) else ()

    instructions = variation.get("instructions")
    if not isinstance(instructions, str):
        instructions = None

    return AgentConfig(
        agent_key=agent_key,
        enabled=bool(meta.get("enabled", False)),
        variation_key=meta.get("variationKey") or None,
        version=meta.get("version"),
        model=model_raw.get("name"),
        model_parameters=model_raw.get("parameters") or {},
        instructions=instructions,
        declared_tools=declared_tools,
        source=source,
        reason=reason,
    )


class AgentConfigSource(Protocol):
    """Where an agent's instructions come from.

    Narrow on purpose, and symmetric with `CapabilityProvider`: a source
    supplies text, it does not decide whether an agent runs. That is the
    capability set's job.
    """

    def fetch(self, agent_key: str, context: dict[str, Any]) -> AgentConfig: ...


class OfflineAgentConfigs:
    """Supplies nothing. Section 6.10 step 4, for agent text.

    No instructions means no agents, and `baseline` already has planner,
    memory, and verification off, so the run falls back to the deterministic
    spine and a manually reviewed manifest. Less helpful, not less correct.
    """

    def __init__(self, reason: str = "NO_SDK_KEY") -> None:
        self._reason = reason

    def fetch(self, agent_key: str, context: dict[str, Any]) -> AgentConfig:
        return AgentConfig(
            agent_key=agent_key, source="unavailable", reason=self._reason
        )


class SnapshotAgentConfigs:
    """Reads the committed snapshot. Section 6.10 step 1, bootstrap from cache.

    The file is read once and held, because a run pulls every agent from it and
    re-reading per agent would let the file change underneath a single run.
    """

    def __init__(self, path: Path | str = DEFAULT_SNAPSHOT_PATH) -> None:
        self.path = Path(path)
        self._agents: dict[str, Any] | None = None
        self._reason = "SNAPSHOT"
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._reason = "NO_SNAPSHOT"
            return
        except (OSError, json.JSONDecodeError):
            # An unreadable cache is a degraded path, not a failed run. The
            # hash mismatch in `from_snapshot` is different: that file parsed
            # fine and actively misdescribes itself.
            self._reason = "SNAPSHOT_UNREADABLE"
            return

        if not isinstance(raw, dict) or raw.get("schema_version") != (
            SNAPSHOT_SCHEMA_VERSION
        ):
            self._reason = "SNAPSHOT_SCHEMA_MISMATCH"
            return

        agents = raw.get("agents")
        self._agents = agents if isinstance(agents, dict) else None
        if self._agents is None:
            self._reason = "SNAPSHOT_UNREADABLE"

    def fetch(self, agent_key: str, context: dict[str, Any]) -> AgentConfig:
        entry = (self._agents or {}).get(agent_key)
        if not isinstance(entry, dict):
            return AgentConfig(
                agent_key=agent_key,
                source="unavailable",
                reason=self._reason if self._agents is None else "NOT_IN_SNAPSHOT",
            )
        return AgentConfig.from_snapshot(agent_key, entry)


class LaunchDarklyAgentConfigs:
    """Retrieves AI Configs from LaunchDarkly.

    Reads the raw flag value rather than going through `LDAIClient`, for the
    variation key and the un-rendered guarantee described in the module
    docstring. `LDAIClient.agent_config()` is still the right call at
    invocation time, where rendered text is what you want.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch(self, agent_key: str, context: dict[str, Any]) -> AgentConfig:
        from .context import to_ld_context

        detail = self._client.variation_detail(agent_key, to_ld_context(context), {})
        reason = reason_code(detail.reason)
        if detail.is_default_value():
            # LD answered with the fallback, so there is no config to record.
            # Distinguished from a served-but-empty config on purpose.
            return AgentConfig(
                agent_key=agent_key, source="unavailable", reason=reason
            )
        return _from_variation(
            agent_key, detail.value, source="launchdarkly", reason=reason
        )


class ChainedAgentConfigs:
    """Tries each source in order and takes the first usable answer.

    This is section 6.10's ordering made concrete: LaunchDarkly, then the
    cached snapshot, then nothing. `available` rather than "no exception" is
    the test, because a served-but-disabled config is a real answer from LD
    and should not silently fall through to stale cached text.
    """

    def __init__(self, *sources: AgentConfigSource) -> None:
        if not sources:
            raise ValueError("ChainedAgentConfigs needs at least one source.")
        self._sources = sources

    def fetch(self, agent_key: str, context: dict[str, Any]) -> AgentConfig:
        last = None
        for source in self._sources:
            config = source.fetch(agent_key, context)
            if config.available or config.source == "launchdarkly":
                return config
            last = config
        assert last is not None  # sources is non-empty
        return last


def fetch_agent_configs(
    source: AgentConfigSource,
    context_for_agent: Any,
    agent_keys: tuple[str, ...] = AGENT_KEYS,
) -> dict[str, AgentConfig]:
    """Pull every agent config, each under its own stage context.

    `context_for_agent` is a callable taking a stage key, so each agent is
    evaluated against the `stage` kind it will actually run under. Evaluating
    all four under one context would make per-agent targeting silently
    ineffective, which is the whole point of the multi-context in 6.6.
    """
    return {
        key: source.fetch(key, context_for_agent(AGENT_STAGES[key]))
        for key in agent_keys
    }


def snapshot_document(configs: dict[str, AgentConfig]) -> dict[str, Any]:
    """The committed artifact. Design 6.4 mitigation 3.

    Carries no run id and no timestamp, so the file changes if and only if the
    LD configs changed. That is what makes `git log` on this file a readable
    history of instruction edits rather than one commit per run. Which run saw
    which snapshot is recorded in the ledger, where per-run facts belong.
    """
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "agents": {key: configs[key].to_snapshot() for key in sorted(configs)},
    }


def write_snapshot(
    configs: dict[str, AgentConfig], path: Path | str = DEFAULT_SNAPSHOT_PATH
) -> bool:
    """Write the snapshot. Returns whether the file's contents changed.

    Unchanged content is not rewritten, so a run that found nothing new in LD
    leaves a clean working tree and there is nothing to commit.

    Configs that came back unavailable are not written: overwriting good cached
    instructions with nothing, on the one run where LD was unreachable, would
    destroy the fallback exactly when it is needed.
    """
    path = Path(path)
    usable = {key: config for key, config in configs.items() if config.available}

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            existing = loaded.get("agents", {}) if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}

    merged = dict(existing)
    merged.update({key: config.to_snapshot() for key, config in usable.items()})
    document = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "agents": {key: merged[key] for key in sorted(merged)},
    }

    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == serialized:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
    return True
