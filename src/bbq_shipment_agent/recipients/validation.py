"""B2: validate addresses. Design section 4, Phase B.

"Each record through Shippo address validation. Three-way outcome: clean,
correctable, failed. Deterministic."

Deterministic is the operative word. No model runs here -- the answer is
three-valued and mechanical, and design 2's "agency is a cost" applies. The
agent that reasons about a broken address is B3, which is offered validation
as a *tool*; this stage just asks and routes.

## Why this stage is load-bearing rather than cosmetic

Quoting happens against whatever address it is given. A carrier will price a
parcel to a mistyped ZIP and return a perfectly real rate and transit estimate
for the wrong destination, and nothing downstream can tell. Measured against
the live validator, `94110` for the origin came back corrected to `94117` -- a
different neighbourhood, a different lane, a different price. Every cost on a
manifest built from unvalidated addresses rests on the operator having typed
them correctly.

So B2 runs before C2, and the address the validator returns is the address
that gets quoted.

## Clean versus correctable

The validator always enriches: `20500` comes back `20500-0005`. Comparing raw
strings would therefore classify every address as correctable and route the
whole run to a human. Comparison normalises -- case-folded, whitespace
collapsed, ZIP truncated to five digits -- so only a *material* change counts.

## The mode is a runtime dial

`ValidationMode` decides who adjudicates a correctable address, and it is a
LaunchDarkly flag so that decision does not need a deploy. Design 6.6's
`shipment` context kind would allow this per recipient -- strict only for
addresses with prior failures -- but capabilities currently resolve once at
A1, so today it is a run-level setting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from ..capabilities import ValidationMode
from ..planning.manifest import Excluded
from ..planning.rates import Address
from .record import Recipient

_WHITESPACE = re.compile(r"\s+")


class ValidationOutcome(StrEnum):
    """Design 4's three-way outcome, plus the absence of validation.

    `SKIPPED` is not a fourth verdict -- it records that `ValidationMode.OFF`
    meant no verdict was sought. Collapsing it into `CLEAN` would let a
    manifest claim addresses were checked when they were not.
    """

    CLEAN = "clean"
    CORRECTABLE = "correctable"
    FAILED = "failed"
    SKIPPED = "skipped"


class AddressValidationUnavailable(Exception):
    """Validation could not run. Stops the run rather than degrading.

    Same posture as quoting, and for the same reason: an unvalidated address
    produces a real-looking quote for the wrong place. `ValidationMode.OFF` is
    how you deliberately skip validation; a failed validator is not.
    """


@dataclass(frozen=True)
class ValidationResult:
    """One address, as submitted and as the validator returned it."""

    submitted: Address
    corrected: Address
    outcome: ValidationOutcome
    messages: tuple[str, ...] = ()

    @property
    def usable(self) -> Address:
        """The address to quote against."""
        return self.corrected

    @property
    def advisory(self) -> bool:
        """The validator said something about an address it did not change.

        Measured live: Shippo returned *"Street address (directional or suffix
        only) was corrected to validate the address. Please check this
        correction prior to using address."* while returning a `street1` byte
        for byte identical to what was sent. It claims a correction; the
        fields show none.

        `classify` is right to call that CLEAN -- no material field moved, and
        treating enrichment as a correction would route every run to a human.
        But the claim is worth showing, and it was going nowhere: the messages
        sat on this record and nothing read them. Design 10 records the three
        options; this is the one that keeps the three-way enum intact and puts
        the text where the operator is already looking.
        """
        return self.outcome is ValidationOutcome.CLEAN and bool(self.messages)


def _normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", value.strip().lower())


def _material_fields(address: Address) -> tuple[str, ...]:
    """The parts of an address a change to would mean a different doorstep.

    ZIP is truncated to five digits because the validator always appends the
    +4, and treating enrichment as a correction would route every address to a
    human.
    """
    return (
        _normalize(address.street1),
        _normalize(address.city),
        _normalize(address.state),
        _normalize(address.zip)[:5],
        _normalize(address.country),
    )


def classify(submitted: Address, corrected: Address, is_valid: bool) -> ValidationOutcome:
    """Design 4's three-way outcome."""
    if not is_valid:
        return ValidationOutcome.FAILED
    if _material_fields(submitted) == _material_fields(corrected):
        return ValidationOutcome.CLEAN
    return ValidationOutcome.CORRECTABLE


class AddressValidator(Protocol):
    """Checks one address. The seam Shippo sits behind."""

    def validate(self, address: Address) -> ValidationResult: ...


class ShippoAddressValidator:
    """Live validation, with the same on-disk cache the quoter uses."""

    def __init__(
        self, api_key: str | None = None, cache_path: Path | str | None = None
    ) -> None:
        import os

        key = api_key if api_key is not None else os.environ.get("SHIPPO_API_KEY", "")
        if not key:
            raise AddressValidationUnavailable(
                "SHIPPO_API_KEY is unset or empty. Validation is not optional: "
                "an unvalidated address yields a real quote for the wrong place."
            )
        self._key = key
        self._client: Any = None
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict[str, Any]] = {}
        if self._cache_path and self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def _sdk(self) -> Any:
        if self._client is None:
            from shippo import Shippo

            self._client = Shippo(api_key_header=self._key)
        return self._client

    def validate(self, address: Address) -> ValidationResult:
        key = address.cache_key()
        if key in self._cache:
            return _result_from_dict(address, self._cache[key])

        from shippo.models import components as c

        answer = self._sdk().addresses.create(
            c.AddressCreateRequest(
                name=address.name, street1=address.street1, city=address.city,
                state=address.state, zip=address.zip, country=address.country,
                validate=True,
            )
        )
        results = answer.validation_results
        row = {
            "is_valid": bool(getattr(results, "is_valid", False)),
            "street1": answer.street1 or address.street1,
            "city": answer.city or address.city,
            "state": answer.state or address.state,
            "zip": answer.zip or address.zip,
            "country": answer.country or address.country,
            "messages": [
                str(getattr(m, "text", "") or "") for m in (getattr(results, "messages", None) or [])
            ],
        }
        self._cache[key] = row
        self._flush()
        return _result_from_dict(address, row)

    def _flush(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8"
        )


class RecordedAddressValidator:
    """Replays validations captured from the live API. For tests.

    Like `RecordedQuoter`: real answers, not invented ones, and an unrecorded
    address raises rather than being assumed clean -- which would quietly turn
    a test fixture into a rubber stamp.
    """

    def __init__(self, recording: dict[str, dict[str, Any]]) -> None:
        self._recording = recording

    @classmethod
    def from_file(cls, path: Path | str) -> RecordedAddressValidator:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self, address: Address) -> ValidationResult:
        key = address.cache_key()
        if key not in self._recording:
            raise AddressValidationUnavailable(
                f"no recorded validation for {key}. Re-record against the live "
                "API rather than assuming the address is fine."
            )
        return _result_from_dict(address, self._recording[key])


def _result_from_dict(submitted: Address, row: dict[str, Any]) -> ValidationResult:
    corrected = Address(
        name=submitted.name,
        street1=row["street1"], city=row["city"], state=row["state"],
        zip=row["zip"], country=row["country"],
    )
    return ValidationResult(
        submitted=submitted,
        corrected=corrected,
        outcome=classify(submitted, corrected, row["is_valid"]),
        messages=tuple(row.get("messages", [])),
    )


@dataclass(frozen=True)
class ValidationReport:
    """B2's output: who may proceed, who needs a human, and why."""

    eligible: tuple[Recipient, ...]
    escalated: tuple[Excluded, ...]
    #: The same escalations as full records, carrying provenance. `escalated`
    #: is the human-facing list and cannot be re-read; this is what B3 works
    #: on, because repairing an address means going back to the image it came
    #: from and `Excluded` is three strings.
    for_repair: tuple[Recipient, ...]
    #: recipient key -> what the validator said. Kept for every shipment,
    #: including the ones that passed, because "this address was checked and
    #: was already correct" is a different claim from "this address was never
    #: checked" and the manifest should be able to tell them apart.
    results: dict[str, ValidationResult]
    mode: ValidationMode

    @property
    def corrected_count(self) -> int:
        return sum(
            1 for r in self.results.values()
            if r.outcome is ValidationOutcome.CORRECTABLE
        )

    def advisories(self) -> dict[str, tuple[str, ...]]:
        """Recipient key -> what the validator said about a clean address.

        Only the clean ones: a correctable address already surfaces its
        messages through the correction itself, and a failed one through the
        escalation reason. These are the ones that would otherwise vanish.
        """
        return {
            key: result.messages
            for key, result in self.results.items()
            if result.advisory
        }


def validate_recipients(
    recipients: tuple[Recipient, ...],
    validator: AddressValidator,
    mode: ValidationMode = ValidationMode.STANDARD,
) -> ValidationReport:
    """B2. Route every shipment on the validator's verdict.

    `standard` applies the validator's correction and escalates only hard
    failures. `strict` escalates anything that was not already correct, which
    is the setting for a run against an unfamiliar recipient list. `off`
    skips validation entirely and ships what it was given.
    """
    if mode is ValidationMode.OFF:
        return ValidationReport(
            eligible=recipients,
            escalated=(),
            for_repair=(),
            results={
                s.key: ValidationResult(
                    submitted=s.address,
                    corrected=s.address,
                    outcome=ValidationOutcome.SKIPPED,
                    messages=("validation-mode is off; address used as supplied",),
                )
                for s in recipients
            },
            mode=mode,
        )

    eligible: list[Recipient] = []
    escalated: list[Excluded] = []
    for_repair: list[Recipient] = []
    results: dict[str, ValidationResult] = {}

    for shipment in recipients:
        result = validator.validate(shipment.address)
        results[shipment.key] = result

        escalate = result.outcome is ValidationOutcome.FAILED or (
            mode is ValidationMode.STRICT
            and result.outcome is ValidationOutcome.CORRECTABLE
        )
        if escalate:
            detail = "; ".join(result.messages) or "no detail from the validator"
            escalated.append(
                Excluded(
                    recipient_key=shipment.key,
                    name=shipment.name,
                    reason=f"address {result.outcome.value}: {detail}",
                )
            )
            for_repair.append(shipment)
            continue

        # The corrected address is what gets quoted. Replacing it here rather
        # than at C2 means there is one address on the shipment from this
        # point on, and no way to price the submitted one by accident.
        from dataclasses import replace

        eligible.append(replace(shipment, address=result.usable))

    return ValidationReport(
        eligible=tuple(eligible),
        escalated=tuple(escalated),
        for_repair=tuple(for_repair),
        results=results,
        mode=mode,
    )
