"""Capability configuration. Design section 6.

LaunchDarkly is the **sole source of truth** for the capability flags, and each
one is evaluated **live**, under its own `stage` context, at the point the run
reaches the stage that reads it:

    planner-mode          -> B3   (stage: address_repair)
    validation-mode       -> B2   (stage: address_validation)
    verification-enabled  -> D1   (stage: manifest_verification)
    pipeline-kill-switch  -> A1   (stage: run_init)

There is no committed profile file and no repo-side resolution step. A run that
cannot reach LaunchDarkly falls back to a code-level default per flag -- the
value the deterministic spine is safe to run on -- rather than to a profile in
a YAML file. `baseline` behaviour (planner off, validation standard,
verification off) is exactly that set of defaults.

Every value LaunchDarkly serves is coerced through a `StrEnum` before it
reaches the ledger. A value that is not a member of the enum is discarded in
favour of the default and the reason records what was rejected: an unedited
console variation still holding a placeholder must not stop a shipping run.
The coercion is also the secrets guarantee -- `flag_payload` and `cap_snapshot`
are structurally incapable of carrying free text, because the only values that
survive are enum members (CLAUDE.md).

## What used to be here

`config/capabilities.yaml`, `CapabilityConfig`, named profiles, the
`resolve()` proposal pipeline, the declarative `Prerequisite` machinery, and
the kill switch read from a committed file are all gone. LaunchDarkly serves
the values directly and evaluates them live per stage, so the repo no longer
holds a proposal layer that LD merely feeds, a named-profile bundle, or a
shadow-run gate. Named profiles move into LaunchDarkly targeting rules keyed on
the `profile` attribute -- which the run still carries and records, now purely
as a targeting label rather than a repo-side bundle.

`authority` and `memory` were removed earlier for gating nothing; see git
history. The rule that removed them still holds: every capability below is read
by a stage.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, Protocol

from .context import (
    STAGE_ADDRESS_REPAIR,
    STAGE_ADDRESS_VALIDATION,
    STAGE_MANIFEST_VERIFICATION,
    reason_code,
    to_ld_context,
)
from .hashing import canonical_hash


class PlannerMode(StrEnum):
    """Not a boolean. `shadow` runs the planner path and logs its output
    without acting on it, which is how a capability earns promotion."""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


class ValidationMode(StrEnum):
    """B2's strictness. Not a boolean, for the same reason `planner` is not.

    The interesting question is not whether validation runs, it is who
    adjudicates a *correctable* address -- one the validator can fix but did
    not receive correctly. `standard` lets the correction stand; `strict`
    sends it to a human. That is a real operational dial: strict for a first
    run against an unfamiliar recipient list, standard once the data is
    trusted, and neither should need a deploy.
    """

    OFF = "off"
    STANDARD = "standard"
    STRICT = "strict"


class VerificationMode(StrEnum):
    OFF = "off"
    ON = "on"



class CapabilityConfigError(Exception):
    """A capability value cannot be trusted to be what it says.

    Raised only by `CapabilitySet.from_mapping`, which is the strict reader
    used when a full set is reconstructed from stored values (a snapshot, a
    test fixture). A value served live by LaunchDarkly never raises -- it is
    coerced and, if invalid, discarded in favour of the default. See `_coerce`.
    """


class KillSwitchEngaged(Exception):
    """`pipeline-kill-switch` is on in LaunchDarkly. The run must not start."""


def _coerce_scalar(name: str, enum_type: type[StrEnum], value: Any) -> StrEnum:
    """Strictly coerce a stored scalar to an enum member, or raise.

    Booleans are mapped before the enum lookup: LaunchDarkly can serve a
    two-state flag as a JSON boolean, and `verification-enabled` in particular
    reads naturally as one. `on`/`off` is the enum's spelling, so a bare `true`
    becomes `on` rather than failing the lookup.
    """
    if isinstance(value, bool):
        value = "on" if value else "off"
    if not isinstance(value, str):
        raise CapabilityConfigError(
            f"{name}: expected a string, got {type(value).__name__} ({value!r})."
        )
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum_type)
        raise CapabilityConfigError(
            f"{name}: {value!r} is not one of: {allowed}."
        ) from exc


@dataclass(frozen=True)
class Capability:
    """One capability: how it is named, served, validated, and staged.

    A single descriptor so a new capability is declared in exactly one place --
    its ledger name, its LaunchDarkly flag key, the enum that validates what LD
    serves, the `stage` context it is evaluated under, and the default the
    spine falls back to offline.
    """

    #: The name used on the ledger snapshot and the fingerprint: "planner".
    name: str
    #: The LaunchDarkly flag key: "planner-mode".
    flag_key: str
    #: The enum that validates LD's value and constrains what reaches the ledger.
    enum: type[StrEnum]
    #: The `stage` context kind this flag is evaluated under, so a targeting
    #: rule written against the stage actually fires.
    stage: str
    #: The value used when LaunchDarkly is unreachable. The deterministic spine
    #: is safe on this: it is the `baseline` value.
    default: StrEnum


#: Every capability, keyed by ledger name. The one place a capability is
#: declared. Each is read by exactly one stage -- that is the bar a capability
#: has to clear to be here (see the module docstring on removals).
CAPABILITIES: dict[str, Capability] = {
    "planner": Capability(
        "planner", "planner-mode", PlannerMode, STAGE_ADDRESS_REPAIR, PlannerMode.OFF
    ),
    "validation": Capability(
        "validation",
        "validation-mode",
        ValidationMode,
        STAGE_ADDRESS_VALIDATION,
        ValidationMode.STANDARD,
    ),
    "verification": Capability(
        "verification",
        "verification-enabled",
        VerificationMode,
        STAGE_MANIFEST_VERIFICATION,
        VerificationMode.OFF,
    ),
}

#: Capability name -> the enum that validates it. Derived from `CAPABILITIES`,
#: kept because a test pins this set: a capability nothing consults is
#: decorative, and adding one whose values are not a `StrEnum` breaks the
#: secrets guarantee rather than silently widening what reaches the ledger.
CAPABILITY_TYPES: dict[str, type[StrEnum]] = {
    name: cap.enum for name, cap in CAPABILITIES.items()
}

#: LD flag key -> capability name. Derived, for display and for a caller that
#: needs to go the other way.
CAPABILITY_FLAGS: dict[str, str] = {
    cap.flag_key: name for name, cap in CAPABILITIES.items()
}

#: The kill switch is a boolean flag, not a capability with an enum, so it is
#: not in `CAPABILITIES`. It is evaluated at A1 under `stage: run_init` and
#: defaults to *off* when LaunchDarkly is unreachable: an LD outage must not
#: brick an offline run, and there is no money to spend and no label to buy, so
#: fail-open is the safe direction. Design 6.8.
KILL_SWITCH_FLAG = "pipeline-kill-switch"
KILL_SWITCH_DEFAULT = False

#: The default targeting label a run carries when none is named. `profile` is
#: no longer a repo-side bundle of values -- it is just a `run` attribute LD
#: targets on, so this is only a label, kept as the one operators already know.
DEFAULT_PROFILE = "baseline"


@dataclass(frozen=True)
class CapabilitySet:
    """The capability values a run actually operated under.

    Assembled from the live per-stage evaluations once all three have run. Its
    only jobs are the snapshot and the fingerprint: naming the equivalence
    class "these values" so shipment rows can point at it rather than copy it.
    """

    planner: PlannerMode
    validation: ValidationMode
    verification: VerificationMode

    @classmethod
    def from_mapping(cls, values: dict[str, Any], *, source: str) -> CapabilitySet:
        """Strictly reconstruct a full set from stored values.

        Used when a complete set is read back -- a snapshot or a test fixture --
        where a missing or malformed value is a real error rather than a live
        LD value to coerce past. Live evaluation never routes through here.
        """
        parsed: dict[str, StrEnum] = {}
        for name, enum_type in CAPABILITY_TYPES.items():
            if name not in values:
                raise CapabilityConfigError(f"{source}: missing capability {name!r}.")
            parsed[name] = _coerce_scalar(f"{source}.{name}", enum_type, values[name])
        unknown = set(values) - set(CAPABILITY_TYPES)
        if unknown:
            raise CapabilityConfigError(
                f"{source}: unknown capability {sorted(unknown)}."
            )
        return cls(**parsed)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name).value for f in fields(self)}

    def replace(self, **changes: StrEnum) -> CapabilitySet:
        return CapabilitySet(**{**self.__dict__, **changes})  # type: ignore[arg-type]

    def fingerprint(self) -> str:
        """Stable identity for this exact capability set.

        Names the equivalence class "these values", deliberately ignoring how
        they were arrived at. This is the join key shipment rows carry in place
        of a repeated snapshot blob; the run row holds the values themselves.
        """
        return f"cap-{canonical_hash(self.to_mapping())}"


@dataclass(frozen=True)
class FlagEvaluation:
    """One flag's evaluated value, and where it came from."""

    value: Any
    #: "launchdarkly" | "offline"
    source: str
    reason: str


class FlagGate(Protocol):
    """The live flag-evaluation seam.

    Deliberately narrow: it evaluates one flag against one context and returns
    the raw value plus a reason. Coercion, defaulting and ledger recording are
    the caller's, so the two implementations below cannot disagree about them.
    """

    def evaluate(
        self, flag_key: str, context: dict[str, Any], default: Any
    ) -> FlagEvaluation: ...


class OfflineGate:
    """Serves the default for every flag, so the run operates on the spine's
    safe values alone.

    Used when there is no SDK key and no reachable client. Section 6.10 makes
    unreachable a normal path for a CLI that cold-starts every run.
    """

    def __init__(self, reason: str = "OFFLINE") -> None:
        self._reason = reason

    def evaluate(
        self, flag_key: str, context: dict[str, Any], default: Any
    ) -> FlagEvaluation:
        return FlagEvaluation(value=default, source="offline", reason=self._reason)


class LaunchDarklyGate:
    """Evaluates a flag live against LaunchDarkly, under the caller's context.

    A thin wrapper over `variation_detail`: it passes the code default as the
    SDK fallback, so an unreachable flag or a null variation comes back as the
    default and the reason names why. Coercion is the caller's -- whatever LD
    returns is still validated against the capability's enum.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def evaluate(
        self, flag_key: str, context: dict[str, Any], default: Any
    ) -> FlagEvaluation:
        ld_context = to_ld_context(context)
        detail = self._client.variation_detail(flag_key, ld_context, default)
        return FlagEvaluation(
            value=detail.value,
            source="launchdarkly",
            reason=reason_code(detail.reason),
        )


def _coerce(capability: Capability, raw: Any) -> tuple[StrEnum, str | None]:
    """Coerce a served value through the capability's enum.

    Returns the value to use and, when the served value was rejected, the
    reason naming what it was. A rejected value degrades to the default rather
    than raising: a live LD value holding a console placeholder must not be the
    one failure mode that stops a shipping run, and the profile is gone, so the
    default is the only thing left to stand on.
    """
    if isinstance(raw, bool):
        raw = "on" if raw else "off"
    try:
        return capability.enum(str(raw)), None
    except ValueError:
        return capability.default, f"FLAG_VALUE_INVALID:{raw!r}"


def evaluate_capability(
    capability: Capability, gate: FlagGate, context: dict[str, Any]
) -> tuple[StrEnum, str, str]:
    """Evaluate one capability live and coerce the result.

    Returns `(value, source, reason)`. `source` is the gate's ("launchdarkly"
    or "offline"); `reason` is either the coercion rejection or the gate's
    reason qualified by source, so the ledger records both why LD said what it
    said and whether the repo could use it.
    """
    ev = gate.evaluate(capability.flag_key, context, capability.default.value)
    value, rejected = _coerce(capability, ev.value)
    reason = rejected if rejected is not None else f"{ev.source}:{ev.reason}"
    return value, ev.source, reason


def evaluate_kill_switch(gate: FlagGate, context: dict[str, Any]) -> FlagEvaluation:
    """Evaluate the kill switch live. `True` means stop the run."""
    ev = gate.evaluate(KILL_SWITCH_FLAG, context, KILL_SWITCH_DEFAULT)
    return FlagEvaluation(value=bool(ev.value), source=ev.source, reason=ev.reason)
