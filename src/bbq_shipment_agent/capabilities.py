"""Capability configuration, clamping, and prerequisites. Design section 6.

The split this module enforces: LaunchDarkly proposes capability values, the
repo decides what is permitted. `authority_ceiling` and `kill_switch` are read
only from the committed config file, never from a flag payload, an environment
variable, or a CLI argument -- so changing what the system may do in the world
is always a diff someone can find in `git log`.

Resolution is a pipeline, and every stage records why it did what it did:

    profile defaults -> flag overrides -> authority clamp -> prerequisites

The reasons land on the run record. A run that behaved surprisingly months ago
has to be explainable from the ledger alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .hashing import canonical_hash

DEFAULT_CONFIG_PATH = Path("config/capabilities.yaml")


class PlannerMode(StrEnum):
    """Not a boolean. `shadow` runs the planner path and logs its output
    without acting on it, which is how a capability earns promotion."""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


class MemoryMode(StrEnum):
    OFF = "off"
    READ = "read"
    READ_WRITE = "read_write"


class VerificationMode(StrEnum):
    OFF = "off"
    ON = "on"


class AuthorityLevel(StrEnum):
    """Ordered. `rank` is what the ceiling clamp compares."""

    PROPOSE_ONLY = "propose_only"
    PURCHASE_LABELS = "purchase_labels"

    @property
    def rank(self) -> int:
        return _AUTHORITY_ORDER.index(self)

    def __lt__(self, other: AuthorityLevel) -> bool:  # type: ignore[override]
        return self.rank < other.rank


_AUTHORITY_ORDER: tuple[AuthorityLevel, ...] = (
    AuthorityLevel.PROPOSE_ONLY,
    AuthorityLevel.PURCHASE_LABELS,
)

#: Capability name -> the enum that validates it. Drives parsing and the
#: fingerprint, so a new capability is added in exactly one place.
CAPABILITY_TYPES: dict[str, type[StrEnum]] = {
    "planner": PlannerMode,
    "memory": MemoryMode,
    "verification": VerificationMode,
    "authority": AuthorityLevel,
}


class CapabilityConfigError(Exception):
    """The committed capability config cannot be trusted to be what it says."""


class KillSwitchEngaged(Exception):
    """`kill_switch: true` in the repo config. The run must not start."""


def _coerce_yaml_scalar(name: str, value: Any) -> str:
    """Undo YAML 1.1's bare `off`/`on` -> bool coercion.

    The config quotes these, but an editor dropping the quotes would otherwise
    turn `planner` into a bool in some profiles and a string in others. Failing
    loudly here would punish a cosmetic edit; normalizing keeps the file
    forgiving while the enums below still reject anything genuinely wrong.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        return value
    raise CapabilityConfigError(
        f"{name}: expected a string, got {type(value).__name__} ({value!r})."
    )


@dataclass(frozen=True)
class CapabilitySet:
    """The four capability values a run actually operates under."""

    planner: PlannerMode
    memory: MemoryMode
    verification: VerificationMode
    authority: AuthorityLevel

    @classmethod
    def from_mapping(cls, values: dict[str, Any], *, source: str) -> CapabilitySet:
        parsed: dict[str, StrEnum] = {}
        for name, enum_type in CAPABILITY_TYPES.items():
            if name not in values:
                raise CapabilityConfigError(f"{source}: missing capability {name!r}.")
            raw = _coerce_yaml_scalar(f"{source}.{name}", values[name])
            try:
                parsed[name] = enum_type(raw)
            except ValueError as exc:
                allowed = ", ".join(m.value for m in enum_type)
                raise CapabilityConfigError(
                    f"{source}.{name}: {raw!r} is not one of: {allowed}."
                ) from exc
        unknown = set(values) - set(CAPABILITY_TYPES)
        if unknown:
            raise CapabilityConfigError(
                f"{source}: unknown capability {sorted(unknown)}. "
                "Add it to CAPABILITY_TYPES or remove it from the config."
            )
        return cls(**parsed)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name).value for f in fields(self)}

    def replace(self, **changes: StrEnum) -> CapabilitySet:
        return CapabilitySet(**{**self.__dict__, **changes})  # type: ignore[arg-type]

    def fingerprint(self) -> str:
        """Stable identity for this exact capability set.

        Names the equivalence class "these four values", deliberately ignoring
        how they were arrived at. `ResolvedCapabilities.reasons` varies
        independently -- two runs can reach the same capabilities by different
        routes -- so folding reasons in would split a class that is genuinely
        one thing.

        This is the join key shipment rows carry in place of a repeated
        snapshot blob; the run row holds the values themselves.
        """
        return f"cap-{canonical_hash(self.to_mapping())}"


@dataclass(frozen=True)
class Prerequisite:
    """One declarative capability dependency from the config."""

    id: str
    when: dict[str, Any]
    requires: dict[str, Any]
    demote: dict[str, str]
    description: str = ""

    def applies_to(self, capabilities: CapabilitySet) -> bool:
        for key, expected in self.when.items():
            if key == "authority_above":
                if not (AuthorityLevel(expected) < capabilities.authority):
                    return False
            elif key in CAPABILITY_TYPES:
                if getattr(capabilities, key).value != expected:
                    return False
            else:
                raise CapabilityConfigError(
                    f"prerequisite {self.id}: unknown `when` key {key!r}."
                )
        return True

    def is_satisfied(self, capabilities: CapabilitySet, *, shadow_runs: int) -> bool:
        for key, expected in self.requires.items():
            if key == "shadow_runs_at_least":
                if shadow_runs < int(expected):
                    return False
            elif key in CAPABILITY_TYPES:
                if getattr(capabilities, key).value != expected:
                    return False
            else:
                raise CapabilityConfigError(
                    f"prerequisite {self.id}: unknown `requires` key {key!r}."
                )
        return True


@dataclass(frozen=True)
class CapabilityConfig:
    """The parsed, committed `config/capabilities.yaml`."""

    profiles: dict[str, CapabilitySet]
    default_profile: str
    authority_ceiling: AuthorityLevel
    kill_switch: bool
    prerequisites: tuple[Prerequisite, ...] = ()

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> CapabilityConfig:
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise CapabilityConfigError(
                f"{path}: not found. Authority and the kill switch are only ever "
                "read from this file, so the run cannot proceed without it."
            ) from exc
        except yaml.YAMLError as exc:
            raise CapabilityConfigError(f"{path}: {exc}") from exc

        profiles_raw = raw.get("profiles") or {}
        if not profiles_raw:
            raise CapabilityConfigError(f"{path}: no profiles defined.")
        profiles = {
            name: CapabilitySet.from_mapping(values, source=f"{path}:profiles.{name}")
            for name, values in profiles_raw.items()
        }

        default_profile = _coerce_yaml_scalar(
            "default_profile", raw.get("default_profile", "baseline")
        )
        if default_profile not in profiles:
            raise CapabilityConfigError(
                f"{path}: default_profile {default_profile!r} is not a defined profile."
            )

        ceiling_raw = _coerce_yaml_scalar(
            "authority_ceiling", raw.get("authority_ceiling", "propose_only")
        )
        try:
            ceiling = AuthorityLevel(ceiling_raw)
        except ValueError as exc:
            raise CapabilityConfigError(
                f"{path}: authority_ceiling {ceiling_raw!r} is not a known level."
            ) from exc

        kill_switch = raw.get("kill_switch", False)
        if not isinstance(kill_switch, bool):
            raise CapabilityConfigError(
                f"{path}: kill_switch must be true or false, got {kill_switch!r}."
            )

        prerequisites = tuple(
            Prerequisite(
                id=entry["id"],
                when=entry.get("when") or {},
                requires=entry.get("requires") or {},
                demote=entry.get("demote") or {},
                description=(entry.get("description") or "").strip(),
            )
            for entry in (raw.get("prerequisites") or [])
        )

        return cls(
            profiles=profiles,
            default_profile=default_profile,
            authority_ceiling=ceiling,
            kill_switch=kill_switch,
            prerequisites=prerequisites,
        )


@dataclass
class ResolvedCapabilities:
    """The outcome of resolution, plus the audit trail that explains it."""

    capabilities: CapabilitySet
    profile: str
    #: capability name -> why it holds the value it does.
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return self.capabilities.fingerprint()

    def to_snapshot(self) -> dict[str, Any]:
        """Ledger-safe capability snapshot.

        Resolved values and reasons only -- never a raw flag payload, because
        this is written to a committed, append-only file.
        """
        return {
            "profile": self.profile,
            "capabilities": self.capabilities.to_mapping(),
            "reasons": dict(self.reasons),
        }


def resolve(
    config: CapabilityConfig,
    *,
    profile: str | None = None,
    overrides: dict[str, str] | None = None,
    shadow_runs: int = 0,
) -> ResolvedCapabilities:
    """Resolve the capability set a run will operate under.

    `overrides` is whatever the flag layer proposed (empty on the offline
    path). It is applied *before* the clamp, never after, so nothing a flag
    payload says can survive the ceiling.

    A proposal the repo cannot use is discarded, not raised on. Everything
    downstream of the flag layer degrades and explains itself -- unmet
    prerequisites demote, an unreachable LD falls back to `baseline` -- and a
    malformed value has no claim to be the exception. An unknown profile name
    still raises: that comes from the caller, not from the network.
    """
    profile_name = profile or config.default_profile
    if profile_name not in config.profiles:
        raise CapabilityConfigError(
            f"unknown profile {profile_name!r}; defined: {sorted(config.profiles)}."
        )

    capabilities = config.profiles[profile_name]
    reasons = {name: f"PROFILE:{profile_name}" for name in CAPABILITY_TYPES}

    for name, raw in (overrides or {}).items():
        if name not in CAPABILITY_TYPES:
            # Nothing to enforce: there is no field for it, so it cannot change
            # what the run does. Recorded rather than dropped, because a
            # provider proposing a capability the repo has never heard of has
            # drifted from the code and that is worth seeing in the ledger.
            reasons[name] = "UNKNOWN_CAPABILITY_IGNORED"
            continue

        enum_type = CAPABILITY_TYPES[name]
        try:
            value = enum_type(_coerce_yaml_scalar(f"override.{name}", raw))
        except (ValueError, CapabilityConfigError):
            # Discarded, never fatal. `CapabilityConfig.load` still raises on a
            # bad value in the committed YAML, where it is a repo problem
            # someone can fix in a commit. This is a value LaunchDarkly handed
            # over at runtime: an unedited variation still holding its console
            # placeholder is likelier than LD being unreachable, and it must
            # not be the one failure mode that stops a shipping run.
            #
            # The profile's value stands and the reason names the value that
            # was rejected, so the console gets fixed rather than guessed at.
            reasons[name] = f"FLAG_VALUE_INVALID:{raw!r}"
            continue

        if value != getattr(capabilities, name):
            capabilities = capabilities.replace(**{name: value})
            reasons[name] = "FLAG_OVERRIDE"

    capabilities, reasons = _clamp_authority(capabilities, reasons, config)
    capabilities, reasons = _apply_prerequisites(
        capabilities, reasons, config, shadow_runs=shadow_runs
    )
    return ResolvedCapabilities(
        capabilities=capabilities, profile=profile_name, reasons=reasons
    )


def _clamp_authority(
    capabilities: CapabilitySet, reasons: dict[str, str], config: CapabilityConfig
) -> tuple[CapabilitySet, dict[str, str]]:
    """Apply the repo ceiling. This is the one-way valve in section 6.5.

    A stale cached payload or a misconfigured targeting rule cannot expand what
    the system is permitted to do, because the clamp runs after every proposal
    and only ever moves authority down.
    """
    if config.authority_ceiling < capabilities.authority:
        reasons = {
            **reasons,
            "authority": f"CLAMPED_TO_CEILING:{config.authority_ceiling.value}",
        }
        capabilities = capabilities.replace(authority=config.authority_ceiling)
    return capabilities, reasons


def _apply_prerequisites(
    capabilities: CapabilitySet,
    reasons: dict[str, str],
    config: CapabilityConfig,
    *,
    shadow_runs: int,
) -> tuple[CapabilitySet, dict[str, str]]:
    """Demote capabilities whose declared dependencies are unmet.

    Re-checked until stable, because one demotion can make another
    prerequisite apply. Bounded by the rule count so a config whose rules
    demote in a cycle fails loudly instead of hanging a shipping run.
    """
    for _ in range(len(config.prerequisites) + 1):
        for rule in config.prerequisites:
            if not rule.applies_to(capabilities):
                continue
            if rule.is_satisfied(capabilities, shadow_runs=shadow_runs):
                continue
            demoted = {
                name: CAPABILITY_TYPES[name](value) for name, value in rule.demote.items()
            }
            if not demoted:
                raise CapabilityConfigError(
                    f"prerequisite {rule.id}: unmet with no `demote` block, so "
                    "there is no defined way to degrade. Add one."
                )
            capabilities = capabilities.replace(**demoted)
            reasons = {
                **reasons,
                **{name: f"PREREQUISITE_UNMET:{rule.id}" for name in demoted},
            }
            break
        else:
            return capabilities, reasons
    raise CapabilityConfigError(
        "prerequisites did not converge; check for rules that demote in a cycle."
    )
