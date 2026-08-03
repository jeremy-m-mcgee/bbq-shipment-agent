"""The spine, end to end. Build order step 3, plus B4 and D1.

A1 -> B2 -> B4 -> C1 -> C2 -> C3 -> C4 -> C5 -> C6 -> D1. Design 11 says
steps 1 through 3 "produce a system that is useful on its own: it will plan a
run correctly and hand over a manifest, with the operator supplying addresses
by hand". Every stage existed before this module; none of them had a caller,
which meant the claim was true of the parts and not of the whole.

Everything through C6 is deterministic and makes no model call. D1 is the one
agent in the sequence, it is gated by `verification-enabled`, and it is
strictly downstream: it reads the finished manifest and reports, and cannot
alter a single field on it (design 10 settles that reading). A run with
verification off is the same run without the critique pass.

B3 sits between B2 and B4, on exactly the set B2 could not pass, and is gated
by `planner-mode`. In `shadow` it runs and its repairs are *not* applied: the
call is paid for, the ledger records what it would have done, and the
escalations stand. That is design 6.7's promotion mechanism rather than a
half-measure -- you cannot tell whether turning a repair loop on is safe
without seeing what it would have repaired.

C4 is in the sequence and makes no model call: it was demoted to ordinary
Python once its only open-ended move turned out to be impossible. See
`planning/remediation`.

B4 sits between B2 and C1 and is the reason `suppressed` on a manifest is no
longer always empty. It runs *after* validation on purpose -- see the comment
at the call site.

## What this module is and is not

It is composition. Every decision belongs to the stage that owns it, and
nothing here computes a cost, a temperature or a ranking. The two judgement
calls it does make are stated below because they are not in any stage:

* **A run that covers nobody is not an exception.** `assemble_manifest`
  refuses to build from a partial plan, and it is right to. The honest output
  is the solve itself, with its partial plans, so the operator can see what
  stranded and why. `PlanResult.manifest` is None in that case, and C4's
  recommendations are on `remediations` either way -- a shipment nothing can
  carry does not stop the others being planned.
* **Planning results are appended to the run record, without `completed_at`.**
  Design 7 makes a ledger line a partial update, so the counts and the
  proposed total cost land on the run row opened at A1. `completed_at` is
  deliberately absent: planning is not a terminal state, dispatch is, and
  `count_shadow_runs` counts only completed runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .agents import (
    ModelClient,
    ModelUnavailable,
    Verification,
    verify_manifest,
)
from .capabilities import PlannerMode, ValidationMode
from .ledger import LedgerWriter, RunRecord
from .planning import (
    Address,
    Excluded,
    Manifest,
    RateQuoter,
    Solve,
    ThermalModel,
    Remediation,
    assemble_manifest,
    remediate_all,
    solve_carriers,
)
from .recipients import (
    AddressValidationUnavailable,
    AddressValidator,
    Roster,
    RepairResult,
    RepairUnavailable,
    SuppressionReport,
    ValidationReport,
    dedupe_recipients,
    repair_addresses,
    to_shipments,
    validate_recipients,
)
from .run import Run


class _NoModel:
    """Stands in when no verifier was injected.

    `verify_manifest` checks `verification-enabled` before it calls anything,
    so with verification off this is never reached. With verification *on* and
    no model supplied, raising here is right: the run asked for a critique pass
    and did not get one, and D1 records that as `unavailable` with this reason
    rather than quietly reporting a clean manifest.
    """

    def complete(self, invocation, prompt):
        raise ModelUnavailable(
            f"verification-enabled is on but no model client was injected for "
            f"{invocation.agent_key}. The CLI builds one from ANTHROPIC_API_KEY."
        )


class _NoValidator:
    """Stands in when `validation-mode` is off, and says so if it is called.

    `validate_shipments` short-circuits before touching the validator in that
    mode, so constructing a live one would open a Shippo connection for a run
    that has decided not to validate. If this ever raises, the short-circuit
    has been broken.
    """

    def validate(self, address: Address):
        raise AddressValidationUnavailable(
            "validation-mode is off, so no validator was constructed, but one "
            "was called. B2's short-circuit is broken."
        )


@dataclass(frozen=True)
class PlanResult:
    """Everything the spine produced, whether or not it produced a manifest."""

    run: Run
    roster: Roster
    validation: ValidationReport
    suppression: SuppressionReport
    solve: Solve
    #: B3's result, or None when `planner-mode` is off or nothing needed
    #: repairing. Present even in shadow, where it was computed and not used.
    repair: RepairResult | None
    #: C4's recommendations, one per shipment nothing can carry. Empty in the
    #: usual case, which is why it is not on the manifest.
    remediations: tuple[Remediation, ...]
    manifest: Manifest | None
    #: Why there is no manifest, when there is none.
    reason: str | None = None
    #: D1's report. Always present: "skipped" is a result, not an absence, and
    #: a caller should not have to distinguish "verification is off" from
    #: "verification ran and found nothing" by checking for None.
    verification: Verification | None = None

    @property
    def escalated(self) -> tuple[Excluded, ...]:
        """Who needs a human. B3's list when it ran and was applied, B2's
        otherwise -- including in shadow, where the repairs were computed and
        deliberately not used."""
        if self.repair is not None and self.applied_repairs:
            return self.repair.escalated
        return self.validation.escalated

    @property
    def applied_repairs(self) -> bool:
        return self.run.capabilities.planner is PlannerMode.ON


def plan_run(
    run: Run,
    roster: Roster,
    *,
    ledger_root: Path | str,
    quoter: RateQuoter,
    validator: AddressValidator | None = None,
    model: ThermalModel | None = None,
    ship_dates: tuple[date, ...] | None = None,
    lane_book: Any = None,
    repairer: Any = None,
    screenshots: Path | str | None = None,
    verifier: ModelClient | None = None,
) -> PlanResult:
    """B2 through C6 against an already-initialized run.

    A1 is the caller's, not this function's: `initialize_run` writes the run
    record and needs the LaunchDarkly wiring the CLI assembles, and keeping
    the two apart means a test can plan against a fixed capability set without
    going near a provider.

    The validator is only constructed by the caller when it will be used --
    `validation-mode` decides that, and it is read off the run's resolved
    capabilities rather than passed in, so the flag cannot be bypassed here.
    """
    mode = run.capabilities.validation
    validation = validate_recipients(
        roster.recipients,
        validator if validator is not None else _NoValidator(),
        mode,
    )

    # B3, on exactly the set B2 could not pass. Gated by `planner-mode`,
    # which is not a boolean: `shadow` runs the loop and logs what it would
    # have done *without acting on it*, which is design 6.7's mechanism for a
    # capability earning promotion. So a shadow run pays for the model call
    # and keeps the escalations, and the ledger records what it would have
    # repaired -- which is the only way to know whether turning it on is safe.
    eligible = validation.eligible
    escalated = validation.escalated
    repair: RepairResult | None = None
    planner = run.capabilities.planner
    if repairer is not None and validation.for_repair and planner is not PlannerMode.OFF:
        try:
            repair = repair_addresses(
                run,
                validation.for_repair,
                validator=validator if validator is not None else _NoValidator(),
                model=repairer,
                screenshots=screenshots,
                ledger_root=ledger_root,
            )
        except RepairUnavailable:
            # No repair loop is a normal path, the same way no verification
            # is: the set stays escalated, which is the safe direction.
            repair = None
        else:
            if planner is PlannerMode.ON:
                eligible = eligible + repair.repaired
                escalated = repair.escalated

    # B4, after B2 rather than before it. Design 4 orders them that way and
    # validation is why it matters: two recipients at one doorstep are only
    # *identical* once the validator has canonicalised both addresses to the
    # same ZIP+4. Deduping the submitted forms would miss a pair that differed
    # by a typo the validator was about to fix.
    suppression = dedupe_recipients(eligible)

    # The B4 -> C1 boundary. Provenance and confidence stop here: phase C has
    # no business re-reading a screenshot, and cannot, because it is not
    # holding one. See `recipients/record`.
    shipments = to_shipments(suppression.eligible)

    solve = solve_carriers(
        shipments,
        roster.origin,
        ship_dates or roster.ship_dates,
        quoter,
        model,
    )

    # C4, and only when C3 left something with nothing feasible anywhere.
    # Deterministic since the demotion -- see planning/remediation.
    remediations = remediate_all(
        shipments,
        solve.infeasible,
        roster.origin,
        ship_dates or roster.ship_dates,
        quoter,
        lane_book=lane_book,
        model=model,
    )

    manifest: Manifest | None = None
    reason: str | None = None
    try:
        manifest = assemble_manifest(
            run.run_id,
            solve,
            suppressed=suppression.suppressed,
            escalated=escalated,
            advisories=validation.advisories(),
            cap_fingerprint=run.cap_fingerprint,
        )
    except ValueError as exc:
        # No covering carrier subset. See the module docstring: this is C4's
        # cue, and C4 does not exist yet, so it is reported rather than raised.
        reason = str(exc)

    _record_planning(
        ledger_root, run, roster, validation, suppression, solve, manifest, mode
    )

    # D1 runs only when there is something to verify. A missing manifest is
    # not a manifest with problems, and asking a read-only critic to review
    # nothing would burn a model call to learn what the caller already knows.
    verification = None
    if manifest is not None:
        verification = verify_manifest(
            run,
            manifest,
            ledger_root=ledger_root,
            model=verifier or _NoModel(),
            input_recipients=tuple(r.key for r in roster.recipients),
        )

    return PlanResult(
        run=run,
        roster=roster,
        validation=validation,
        suppression=suppression,
        repair=repair,
        solve=solve,
        remediations=remediations,
        manifest=manifest,
        reason=reason,
        verification=verification,
    )


def _record_planning(
    ledger_root: Path | str,
    run: Run,
    roster: Roster,
    validation: ValidationReport,
    suppression: SuppressionReport,
    solve: Solve,
    manifest: Manifest | None,
    mode: ValidationMode,
) -> None:
    """Append what planning learned to the run row opened at A1.

    A partial update, per design 7: same merge key, only the fields known now,
    and no `completed_at` because the run has not reached a terminal state.
    `carrier_pair` and `total_cost` describe the *proposed* plan -- nothing has
    been approved. Nothing is ever purchased -- dispatch was removed, design 9.
    """
    reasons = run.evaluation_reasons()
    reasons["validation_mode"] = mode.value
    reasons["validation_corrected"] = validation.corrected_count
    if suppression.consolidated:
        # The packer needs this the other way round from the suppression
        # list: this parcel covers these people, so a card with two names
        # goes in the box.
        reasons["consolidated"] = {
            k: list(v) for k, v in suppression.consolidated.items()
        }
    reasons["available_carriers"] = sorted(solve.available_carriers)
    if solve.messages:
        reasons["carrier_messages"] = [
            f"{m.source}: {m.text}" for m in solve.messages[:20]
        ]

    LedgerWriter(ledger_root).append(
        RunRecord(
            run_id=run.run_id,
            packet_count=roster.packet_count,
            carrier_pair=list(manifest.carriers) if manifest else None,
            total_cost=manifest.total_cost if manifest else None,
            suppressed_count=len(suppression.suppressed),
            escalated_count=len(validation.escalated),
            stranded_count=len(manifest.stranded) if manifest else None,
            evaluation_reasons=reasons,
        )
    )
