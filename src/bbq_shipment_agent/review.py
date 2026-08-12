"""D2: human review. Design section 4, Phase D.

"Questions, changes, approval. Edits re-enter the pipeline at the correct
upstream stage rather than patching the manifest in place."

This module is the mechanism. `review-narrator` is the voice, and it lives in
`agents/narrator.py` — design 6.1 puts stage sequencing, retries and every
deterministic decision in Python, and design 4's edit-handling table is
sequencing. The agent triggers a re-solve; this code re-solves, compares and
classifies; the agent narrates the classification it is handed.

## One mechanism, not three

Design 4 is emphatic that edit handling is a single path:

> Always re-solve from C5, then compare the new optimal pair against the
> previous one.

The three behaviours fall out of that comparison rather than being separate
code paths keyed on edit type. That matters because the interesting case is
not "the operator changed a ship date", it is "changing one ship date moved
the carrier set for twenty-two shipments", and only a full re-solve can tell
you which happened.

## Why a moved carrier set stops and asks

An edit that leaves the optimal subset alone is a local change and is applied
silently with the delta shown. An edit that moves it rewrites service and cost
on every row, and an operator who asked to pin one recipient to Saturday did
not ask for that. So it is proposed, not applied, until they say yes.

Design 3 explains why this is the common case rather than a corner: one
Saturday shipment forces USPS into the set and leaves exactly one free slot
for everyone else. Pinning a single recipient is the most likely edit in the
whole review and the most likely to move the plan.

## Why a thermal violation is refused instead

The other two outcomes are the operator's call. This one is not. Design 4:

> Refuse, explain, offer the nearest feasible alternative. No confirmation
> prompt, since the gate is not the operator's to override.

`propose` returns the nearest feasible ship dates for a refused pin, computed
rather than suggested, so the refusal comes with a way forward.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from .ledger import LedgerWriter, RunRecord, ShipmentRecord, utc_now
from .planning import (
    Excluded,
    Manifest,
    RateQuoter,
    Shipment,
    Solve,
    ThermalModel,
    assemble_manifest,
    shipment_options,
    solve_carriers,
)
from .run import Run


class EditKind(StrEnum):
    """What the operator asked to change.

    Deliberately short. Each entry names an upstream stage the edit re-enters:
    a pin re-enters at C2's enumeration, an exclusion at C1's load definition.
    An address change re-enters at B2 and is not supported yet — it needs a
    re-validation and a re-quote on a lane nothing has priced, which is B3's
    territory rather than a review-time edit.
    """

    SHIP_DATE = "ship_date"
    EXCLUDE = "exclude"


class EditOutcome(StrEnum):
    """Design 4's three behaviours, as a classification of the re-solve."""

    #: Optimal carrier set unchanged. Applied; show the delta on the row.
    UNCHANGED = "unchanged"
    #: Optimal carrier set moved. Proposed only, pending confirmation.
    PAIR_MOVED = "pair_moved"
    #: The edit puts a shipment outside the thermal gate. Refused outright.
    REFUSED = "refused"


class ReviewError(Exception):
    """The review was asked to do something it cannot."""


@dataclass(frozen=True)
class Edit:
    """One requested change."""

    kind: EditKind
    recipient_key: str
    ship_date: date | None = None
    reason: str = ""

    def describe(self) -> str:
        if self.kind is EditKind.SHIP_DATE:
            return f"pin {self.recipient_key} to {self.ship_date}"
        return f"exclude {self.recipient_key}"


@dataclass(frozen=True)
class RowChange:
    """One shipment's assignment before and after an edit, field by field.

    Design 4 asks an unchanged-pair edit to "show the delta on the affected
    row", and a re-solve is free to move more than the field that was edited:
    pinning one recipient to a Saturday changed that person's carrier, service,
    gel pack count and cost at once. A re-rendered table states none of that,
    and diffing two manifests from memory is the comparison design 2 says
    humans do badly.

    Computed from the two solves rather than inferred from the edit kind — the
    same reasoning `propose` uses for the classification itself.
    """

    recipient_key: str
    name: str
    #: (field, before, after), already formatted for reading.
    changes: tuple[tuple[str, str, str], ...]

    def describe(self) -> str:
        moves = ", ".join(f"{field} {before} → {after}" for field, before, after in self.changes)
        return f"{self.name}: {moves}"


@dataclass(frozen=True)
class EditResult:
    """What a proposed edit would do, and whether it has been applied."""

    edit: Edit
    outcome: EditOutcome
    #: Set when the edit was refused by the thermal gate.
    refusal: str | None = None
    #: Ship dates that would work, for a refused pin. Computed, not suggested.
    alternatives: tuple[date, ...] = ()
    carriers_before: tuple[str, ...] = ()
    carriers_after: tuple[str, ...] = ()
    cost_before: float | None = None
    cost_after: float | None = None
    #: Recipients newly stranded by the edit.
    newly_stranded: tuple[str, ...] = ()
    #: Per-shipment moves the re-solve produced. Empty on a refusal, which
    #: changed nothing.
    row_changes: tuple[RowChange, ...] = ()

    @property
    def applied(self) -> bool:
        """Only an unchanged-plan edit takes effect without being confirmed."""
        return self.outcome is EditOutcome.UNCHANGED

    @property
    def needs_confirmation(self) -> bool:
        return self.outcome is EditOutcome.PAIR_MOVED

    @property
    def cost_delta(self) -> float | None:
        if self.cost_before is None or self.cost_after is None:
            return None
        return round(self.cost_after - self.cost_before, 2)

    def describe(self) -> str:
        if self.outcome is EditOutcome.REFUSED:
            offer = (
                f" Feasible dates: {', '.join(d.isoformat() for d in self.alternatives)}."
                if self.alternatives
                else " No ship date in this run works for that shipment."
            )
            return f"Refused. {self.refusal}{offer}"
        if self.outcome is EditOutcome.PAIR_MOVED:
            after = "+".join(self.carriers_after) or "(none)"
            return (
                f"This changes the carrier set for the whole run: "
                f"{'+'.join(self.carriers_before)} -> {after}, "
                f"{self.cost_sentence}. Confirm before it is applied."
            )
        return f"Applied. {self.cost_sentence}.{self._rows_sentence()}"

    @property
    def cost_sentence(self) -> str:
        """The run total, with the number it moved from.

        A bare delta says how far without saying from where, and "+58.17" beside
        a manifest of dollar amounts reads as a unit change. Three cases rather
        than two: an edit can also give coverage *back* to a run that had none,
        and reporting that as "no covering plan after this" would be exactly
        backwards.
        """
        if self.cost_after is None:
            return "no covering plan after this"
        if self.cost_before is None:
            return f"run total ${self.cost_after:.2f}, where there was no covering plan"
        return (
            f"run total ${self.cost_before:.2f} → ${self.cost_after:.2f} "
            f"({self.cost_delta:+.2f})"
        )

    def _rows_sentence(self, limit: int = 3) -> str:
        """The per-row moves, capped so one line stays one line.

        Capped rather than dropped: a pair move can rewrite every row, and a
        sentence listing twenty of them is not read. What is above the cap is
        counted, because a silent truncation reads as "that was all of it".
        """
        if not self.row_changes:
            return ""
        shown = "; ".join(change.describe() for change in self.row_changes[:limit])
        rest = len(self.row_changes) - limit
        more = f"; and {rest} more row(s)" if rest > 0 else ""
        return f" {shown}{more}."


def row_changes(before: Any, after: Any) -> tuple[RowChange, ...]:
    """Field-by-field moves between two carrier plans, keyed on the recipient.

    Only shipments present in both are compared. One that leaves the plan is
    either an exclusion (already named in the excluded list) or newly stranded
    (already named on the result), and reporting it a third time as a "change"
    would say the same thing in a weaker way.
    """
    if before is None or after is None:
        return ()

    def by_key(plan: Any) -> dict[str, Any]:
        return {a.shipment.recipient_key: a for a in plan.assignments}

    old, new = by_key(before), by_key(after)
    changed: list[RowChange] = []
    for key, new_assignment in new.items():
        old_assignment = old.get(key)
        if old_assignment is None:
            continue
        a, b = old_assignment.evaluated.configuration, new_assignment.evaluated.configuration
        fields = (
            ("ship date", a.ship_date.isoformat(), b.ship_date.isoformat()),
            ("carrier", a.carrier, b.carrier),
            ("service", a.service_name, b.service_name),
            ("box", a.box_size.value, b.box_size.value),
            ("gel", str(a.gel_packs), str(b.gel_packs)),
            ("cost", f"${a.cost:.2f}", f"${b.cost:.2f}"),
        )
        moves = tuple((name, was, now) for name, was, now in fields if was != now)
        if moves:
            changed.append(
                RowChange(
                    recipient_key=key,
                    name=new_assignment.shipment.name,
                    changes=moves,
                )
            )
    return tuple(sorted(changed, key=lambda c: c.name))


class TerminalState(StrEnum):
    """Design 4's three endings."""

    APPROVED = "approved"
    APPROVED_WITH_EXCLUSIONS = "approved_with_exclusions"
    REJECTED = "rejected"


class ReviewSession:
    """A live review of one run's manifest.

    Holds the current shipment set and re-solves from C5 on every edit. Not a
    conversation — `agents/narrator.py` is that. This is what the conversation
    is allowed to do.
    """

    def __init__(
        self,
        run: Run,
        shipments: tuple[Shipment, ...],
        origin: Any,
        ship_dates: tuple[date, ...],
        *,
        ledger_root: Path | str,
        quoter: RateQuoter,
        thermal_model: ThermalModel | None = None,
        escalated: tuple[Excluded, ...] = (),
        suppressed: tuple[Excluded, ...] = (),
        advisories: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.run = run
        self.origin = origin
        self.ship_dates = ship_dates
        self.ledger_root = ledger_root
        self._quoter = quoter
        self._model = thermal_model
        self._escalated = escalated
        self._shipments = shipments
        # B2's free-text notes on addresses it classified CLEAN. A fact about
        # the address, not about the solve, so they survive every re-solve
        # unchanged -- and without carrying them the review pane, which is the
        # only manifest the web UI renders on a covering run, silently drops
        # what design 10 decided to show against the row.
        self._advisories = dict(advisories or {})
        # Seeded with B4's suppressions, so an exclusion made during the
        # review joins the same list rather than starting a second one.
        self._excluded: list[Excluded] = list(suppressed)
        self._pending: tuple[Edit, tuple[Shipment, ...]] | None = None
        self.history: list[EditResult] = []
        self.terminal: TerminalState | None = None

        self.solve = self._solve(shipments)
        self.manifest = self._manifest(self.solve)

    # -- state -------------------------------------------------------------

    @property
    def shipments(self) -> tuple[Shipment, ...]:
        return self._shipments

    @property
    def excluded(self) -> tuple[Excluded, ...]:
        return tuple(self._excluded)

    @property
    def carriers(self) -> tuple[str, ...]:
        return self.manifest.carriers if self.manifest else ()

    @property
    def total_cost(self) -> float | None:
        return self.manifest.total_cost if self.manifest else None

    def _solve(self, shipments: tuple[Shipment, ...]) -> Solve:
        return solve_carriers(
            shipments, self.origin, self.ship_dates, self._quoter, self._model
        )

    def _manifest(self, solve: Solve) -> Manifest | None:
        try:
            return assemble_manifest(
                self.run.run_id,
                solve,
                suppressed=tuple(self._excluded),
                escalated=self._escalated,
                cap_fingerprint=self.run.cap_fingerprint,
                advisories=self._advisories,
            )
        except ValueError:
            # No covering subset. The session stays usable so the operator can
            # edit their way back to a plan; `manifest` being None is what the
            # narrator reports.
            return None

    # -- editing -----------------------------------------------------------

    def propose(self, edit: Edit) -> EditResult:
        """Re-solve with the edit applied, classify, and apply if it is safe.

        Design 4's one mechanism. The classification comes from comparing the
        new optimal carrier set against the current one — never from the kind
        of edit, which cannot tell you what a re-solve will do.
        """
        if self.terminal is not None:
            raise ReviewError(f"this review already ended as {self.terminal.value}.")

        candidate = self._apply(edit)
        refusal = self._thermal_refusal(edit, candidate)
        if refusal is not None:
            result = EditResult(
                edit=edit,
                outcome=EditOutcome.REFUSED,
                refusal=refusal,
                alternatives=self._feasible_dates(edit.recipient_key),
                carriers_before=self.carriers,
                cost_before=self.total_cost,
            )
            self.history.append(result)
            return result

        solve = self._solve(candidate)
        after = solve.best
        before_carriers = self.carriers
        after_carriers = after.carriers_used if after else ()
        after_cost = after.total_cost if after else None

        stranded_before = set(self.manifest.stranded) if self.manifest else set()
        newly_stranded = tuple(
            sorted(set(after.stranded) - stranded_before) if after else ()
        )

        moved = after_carriers != before_carriers
        result = EditResult(
            edit=edit,
            outcome=EditOutcome.PAIR_MOVED if moved else EditOutcome.UNCHANGED,
            carriers_before=before_carriers,
            carriers_after=after_carriers,
            cost_before=self.total_cost,
            cost_after=after_cost,
            newly_stranded=newly_stranded,
            # Computed against the solve that is about to be committed, so a
            # pair move carries its row moves into the confirmation banner
            # rather than only after the operator has agreed to them.
            row_changes=row_changes(self.solve.best, after),
        )

        if moved:
            # Proposed, not applied. One edit rewriting service and cost
            # across the whole run is not what the operator asked for.
            self._pending = (edit, candidate)
        else:
            self._commit(edit, candidate, solve)

        self.history.append(result)
        return result

    def confirm(self) -> EditResult:
        """Apply the edit that was held back because it moved the plan."""
        if self._pending is None:
            raise ReviewError("nothing is waiting for confirmation.")
        edit, candidate = self._pending
        self._pending = None
        self._commit(edit, candidate, self._solve(candidate))
        return self.history[-1]

    def discard(self) -> None:
        """Drop a pending edit the operator declined."""
        self._pending = None

    @property
    def pending(self) -> Edit | None:
        return self._pending[0] if self._pending else None

    def _commit(
        self, edit: Edit, shipments: tuple[Shipment, ...], solve: Solve
    ) -> None:
        if edit.kind is EditKind.EXCLUDE:
            removed = next(
                (s for s in self._shipments if s.recipient_key == edit.recipient_key),
                None,
            )
            if removed is not None:
                self._excluded.append(
                    Excluded(
                        recipient_key=removed.recipient_key,
                        name=removed.name,
                        reason=edit.reason or "excluded during review",
                    )
                )
        self._shipments = shipments
        self.solve = solve
        self.manifest = self._manifest(solve)

    def _apply(self, edit: Edit) -> tuple[Shipment, ...]:
        """The shipment set as it would be with the edit in place."""
        if not any(s.recipient_key == edit.recipient_key for s in self._shipments):
            raise ReviewError(
                f"{edit.recipient_key!r} is not in this run. Current: "
                f"{', '.join(s.recipient_key for s in self._shipments)}."
            )

        if edit.kind is EditKind.EXCLUDE:
            return tuple(
                s for s in self._shipments if s.recipient_key != edit.recipient_key
            )

        if edit.ship_date is None:
            raise ReviewError("a ship_date edit needs a date.")
        if edit.ship_date not in self.ship_dates:
            raise ReviewError(
                f"{edit.ship_date.isoformat()} is not a candidate ship date for "
                f"this run ({', '.join(d.isoformat() for d in self.ship_dates)})."
            )
        return tuple(
            replace(s, required_ship_date=edit.ship_date)
            if s.recipient_key == edit.recipient_key
            else s
            for s in self._shipments
        )

    def _thermal_refusal(
        self, edit: Edit, candidate: tuple[Shipment, ...]
    ) -> str | None:
        """Whether the edit leaves its shipment with nothing feasible.

        Only a pin can do this: removing a shipment cannot make another one
        too warm. Checked before the re-solve so the refusal names the gate
        rather than surfacing as an unexplained coverage loss.
        """
        if edit.kind is not EditKind.SHIP_DATE:
            return None
        shipment = next(
            s for s in candidate if s.recipient_key == edit.recipient_key
        )
        feasible, _ = shipment_options(
            shipment, self.origin, self.ship_dates, self._quoter, self._model
        )
        if feasible:
            return None
        return (
            f"{shipment.name} cannot ship on "
            f"{edit.ship_date.isoformat() if edit.ship_date else 'that date'} — no "
            "box size, gel pack count or service arrives at or below 4.4C. That "
            "is a food safety limit enforced in code and is not overridable."
        )

    def _feasible_dates(self, recipient_key: str) -> tuple[date, ...]:
        """Candidate dates this shipment *can* make. The offered alternative."""
        base = next(
            (s for s in self._shipments if s.recipient_key == recipient_key), None
        )
        if base is None:
            return ()
        works: list[date] = []
        for when in self.ship_dates:
            feasible, _ = shipment_options(
                replace(base, required_ship_date=when),
                self.origin,
                self.ship_dates,
                self._quoter,
                self._model,
            )
            if feasible:
                works.append(when)
        return tuple(works)

    # -- terminal ----------------------------------------------------------

    def approve(self) -> TerminalState:
        """Record the plan. Design 4's terminal state, and E2.

        Writes one shipment row per assignment and closes the run record. This
        is where the ledger stops describing a proposal and starts describing
        a decision — and it is the last thing that happens, because dispatch
        was removed (design 9). The operator buys the labels.
        """
        if self.terminal is not None:
            raise ReviewError(f"this review already ended as {self.terminal.value}.")
        if self.manifest is None:
            raise ReviewError(
                "there is no covering plan to approve. Edit until one exists, "
                "or reject."
            )
        if self._pending is not None:
            raise ReviewError(
                f"confirm or discard the pending edit first ({self.pending.describe()})."
            )

        state = (
            TerminalState.APPROVED_WITH_EXCLUSIONS
            if self._excluded
            else TerminalState.APPROVED
        )
        writer = LedgerWriter(self.ledger_root)
        for row in self.manifest.rows:
            writer.append(
                ShipmentRecord(
                    run_id=self.run.run_id,
                    recipient_key=row.recipient_key,
                    name=row.name,
                    validated_address=row.address,
                    box_size=row.box_size,
                    gel_pack_count=row.gel_pack_count,
                    ship_date=row.ship_date.isoformat(),
                    carrier=row.carrier,
                    service=row.service,
                    cost=row.cost,
                    expected_arrival=f"{row.expected_arrival.isoformat()}T00:00:00+00:00",
                    predicted_arrival_temp=row.predicted_arrival_temp_c,
                    thermal_margin=row.thermal_margin_c,
                    cap_fingerprint=self.run.cap_fingerprint,
                )
            )
        self._close(state)
        return state

    def reject(self, reason: str = "") -> TerminalState:
        """End the review without a plan. No shipment rows are written."""
        if self.terminal is not None:
            raise ReviewError(f"this review already ended as {self.terminal.value}.")
        self._close(TerminalState.REJECTED, reason=reason)
        return TerminalState.REJECTED

    def _close(self, state: TerminalState, reason: str = "") -> None:
        reasons = self.run.evaluation_reasons()
        reasons["terminal_state"] = state.value
        reasons["review_edits"] = [
            {"edit": r.edit.describe(), "outcome": r.outcome.value}
            for r in self.history
        ]
        if reason:
            reasons["rejection_reason"] = reason

        LedgerWriter(self.ledger_root).append(
            RunRecord(
                run_id=self.run.run_id,
                carrier_pair=list(self.carriers) or None,
                total_cost=self.total_cost,
                suppressed_count=len(self._excluded),
                packet_count=len(self.manifest.rows) if self.manifest else 0,
                evaluation_reasons=reasons,
                # The run reached a terminal state. `completed_at` is what marks
                # a run finished, and a review that ended is the only thing that
                # completes one now that dispatch is gone.
                completed_at=utc_now(),
            )
        )
        self.terminal = state
