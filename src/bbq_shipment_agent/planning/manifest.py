"""C6: assemble the manifest. Design section 4, Phase C.

"The top-ranked plan in full, grouped by ship date, because that is the shape
of the physical work. Attach a compact comparison of runner-up pairs so the
tradeoff stays visible. Attach the suppression list and the escalation list."

Grouping by ship date is not presentation. A run can span three drop-off days,
and the person doing the packing works one date at a time, so the manifest is
organised the way the work is done rather than the way the solver produced it.

## Runner-ups are part of the artifact, not a footnote

Design 4 attaches the runner-up comparison so the tradeoff stays visible, and
design 2 explains why: the pair solve produces candidate plans differing
simultaneously across cost, coverage, stranded shipments, thermal margin and
forced-carrier interactions, and holding those interactions in working memory
is what humans do badly -- especially at the end of a review. The comparison
is computed here so D2's narration has computed fields to point at rather than
numbers it worked out itself.

## Expected arrival is derived, and the derivation is worth questioning

`expected_arrival` is ship date plus `Configuration.elapsed_transit_days`,
which resolves the carriers' disagreement about what they are quoting: UPS
says "second business day", USPS says "delivery in 2 to 5 days". Both arrive
as a bare integer, and treating them alike understated UPS transit whenever
the journey crossed a weekend.

That mattered beyond presentation -- C3 gates on the same figure as elapsed
time, so the thermal model was asking the gel packs to last longer than it
thought, worst for exactly the Saturday shipments design 3 calls the
highest-leverage case. Saturday *delivery* is still not modelled, which errs
long; for a food-safety gate that is the safe direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .solve import Assignment, CarrierPlan, Solve


@dataclass(frozen=True)
class Excluded:
    """A recipient deliberately not in the plan, and why.

    Covers both of design 4's lists. B2 and B3 produce escalations (address
    could not be validated or repaired). Suppressions come from B4, which is
    deferred to build order step 12, so today the suppression list is empty
    unless a caller supplies one. Design 9 records why there is no time-based
    suppression: a name on the recipient list is a deliberate instruction, not
    something to override on the strength of a date.
    """

    recipient_key: str
    name: str
    reason: str


@dataclass(frozen=True)
class ManifestRow:
    """One shipment, in the form the packer and the reviewer both need."""

    recipient_key: str
    name: str
    address: dict[str, Any]
    box_size: str
    gel_pack_count: int
    ship_date: date
    carrier: str
    service: str
    cost: float
    expected_arrival: date
    predicted_arrival_temp_c: float
    thermal_margin_c: float

    @classmethod
    def from_assignment(cls, assignment: Assignment) -> ManifestRow:
        configuration = assignment.evaluated.configuration
        shipment = assignment.shipment
        transit = configuration.elapsed_transit_days or 0
        return cls(
            recipient_key=shipment.recipient_key,
            name=shipment.name,
            address={
                "street1": shipment.address.street1,
                "city": shipment.address.city,
                "state": shipment.address.state,
                "zip": shipment.address.zip,
                "country": shipment.address.country,
            },
            box_size=configuration.box_size.value,
            gel_pack_count=configuration.gel_packs,
            ship_date=configuration.ship_date,
            carrier=configuration.carrier,
            service=configuration.service_name,
            cost=configuration.cost,
            # Elapsed days, resolved per carrier. See the module docstring.
            expected_arrival=configuration.ship_date + timedelta(days=transit),
            # `+ 0.0` normalizes the negative zero that rounding a value a
            # hair below freezing produces. A manifest reading "-0.00C" looks
            # like a defect to the operator reading it, and the packet that
            # arrives with gel packs still mid-phase-change is the *good* case.
            predicted_arrival_temp_c=round(
                assignment.evaluated.predicted_arrival_temp_c, 2
            )
            + 0.0,
            thermal_margin_c=round(assignment.evaluated.thermal_margin_c, 2),
        )


@dataclass(frozen=True)
class RunnerUp:
    """One rejected carrier subset, summarised for comparison.

    `extra_cost` is the premium over the chosen plan, which is the number the
    tradeoff actually turns on -- an absolute total says little without it.
    """

    carriers: tuple[str, ...]
    total_cost: float
    extra_cost: float
    coverage: int
    stranded: tuple[str, ...]
    forced_by_saturday: bool
    min_thermal_margin_c: float | None

    @property
    def covers_all(self) -> bool:
        return not self.stranded


@dataclass(frozen=True)
class Manifest:
    """The reviewable work package. Design 1's actual deliverable."""

    run_id: str
    rows: tuple[ManifestRow, ...]
    carriers: tuple[str, ...]
    runners_up: tuple[RunnerUp, ...]
    suppressed: tuple[Excluded, ...] = ()
    escalated: tuple[Excluded, ...] = ()
    #: Feasible somewhere but not under the chosen carriers. A tradeoff.
    stranded: tuple[str, ...] = ()
    #: Feasible nowhere. C4's input, and not the same thing as stranded.
    infeasible: tuple[str, ...] = ()
    forced_by_saturday: bool = False
    #: Recipients that can ship *only* on a Saturday, which is what forced a
    #: Saturday-capable carrier into the set. Carried because
    #: `forced_by_saturday` alone says a constraint bound without saying who
    #: bound it, and D2 must trace every claim to a computed field. Asked to
    #: name the recipient with only the boolean available, `review-narrator`
    #: named the wrong one and reasoned from it for two turns.
    saturday_only: tuple[str, ...] = ()
    cap_fingerprint: str | None = None

    @property
    def packet_count(self) -> int:
        return len(self.rows)

    @property
    def total_cost(self) -> float:
        return round(sum(r.cost for r in self.rows), 2)

    @property
    def min_thermal_margin_c(self) -> float | None:
        if not self.rows:
            return None
        return min(r.thermal_margin_c for r in self.rows)

    def by_ship_date(self) -> dict[date, tuple[ManifestRow, ...]]:
        """The plan in the shape of the physical work."""
        grouped: dict[date, list[ManifestRow]] = {}
        for row in sorted(self.rows, key=lambda r: (r.ship_date, r.recipient_key)):
            grouped.setdefault(row.ship_date, []).append(row)
        return {when: tuple(rows) for when, rows in sorted(grouped.items())}

    def packets_by_date(self) -> dict[date, int]:
        return {when: len(rows) for when, rows in self.by_ship_date().items()}

    def accounted_for(self) -> tuple[str, ...]:
        """Every recipient key this manifest says something about.

        D1's first check is that every input recipient appears in exactly one
        of eligible, suppressed or escalated. Providing the union here means
        that check compares against a computed field rather than reassembling
        the set itself.
        """
        return tuple(
            [r.recipient_key for r in self.rows]
            + [e.recipient_key for e in self.suppressed]
            + [e.recipient_key for e in self.escalated]
            + list(self.stranded)
            + list(self.infeasible)
        )


def assemble_manifest(
    run_id: str,
    solve: Solve,
    suppressed: tuple[Excluded, ...] = (),
    escalated: tuple[Excluded, ...] = (),
    cap_fingerprint: str | None = None,
) -> Manifest:
    """C6. Turn the winning plan into the reviewable package.

    Raises when nothing covers the run. That is deliberate: design 4 routes an
    empty feasible set to C4 for remediation, and a manifest built from a
    partial plan would present a document that silently omits recipients as
    though it were complete. The caller decides whether to remediate or to
    review the partial plans directly.
    """
    plan = solve.best
    if plan is None:
        raise ValueError(
            f"no carrier subset covers the run; {len(solve.partial)} partial "
            f"plans and {len(solve.infeasible)} infeasible shipments. "
            "Remediate in C4 rather than publishing an incomplete manifest."
        )

    return Manifest(
        run_id=run_id,
        rows=tuple(ManifestRow.from_assignment(a) for a in plan.assignments),
        carriers=plan.carriers_used,
        runners_up=tuple(_runner_up(p, plan) for p in solve.runners_up),
        suppressed=suppressed,
        escalated=escalated,
        stranded=plan.stranded,
        infeasible=solve.infeasible,
        forced_by_saturday=plan.forced_by_saturday,
        saturday_only=solve.saturday_only,
        cap_fingerprint=cap_fingerprint,
    )


def _runner_up(plan: CarrierPlan, chosen: CarrierPlan) -> RunnerUp:
    return RunnerUp(
        carriers=plan.carriers,
        total_cost=plan.total_cost,
        extra_cost=round(plan.total_cost - chosen.total_cost, 2),
        coverage=plan.coverage,
        stranded=plan.stranded,
        forced_by_saturday=plan.forced_by_saturday,
        min_thermal_margin_c=plan.min_thermal_margin_c,
    )


def render(manifest: Manifest) -> str:
    """A plain-text manifest, for reading before D2 exists.

    Not the review interface -- that is build order step 8. This is the
    minimum needed to look at what the spine produced and judge whether it is
    sensible, which is the whole point of steps 1 through 3.
    """
    lines: list[str] = [
        f"{manifest.run_id}",
        f"  carriers      {'+'.join(manifest.carriers) or '(none)'}"
        + (
            f"   [Saturday-only: {', '.join(manifest.saturday_only)} forced a "
            "Saturday-capable carrier]"
            if manifest.forced_by_saturday
            else ""
        ),
        f"  packets       {manifest.packet_count}",
        f"  total cost    ${manifest.total_cost:,.2f}",
    ]
    if manifest.min_thermal_margin_c is not None:
        lines.append(f"  min margin    {manifest.min_thermal_margin_c:.2f}C")
    if manifest.cap_fingerprint:
        lines.append(f"  capabilities  {manifest.cap_fingerprint}")

    for when, rows in manifest.by_ship_date().items():
        lines.append(f"\n  {when:%a %d %b %Y} — {len(rows)} packet(s)")
        for row in rows:
            lines.append(
                f"    {row.name:<22} {row.carrier:<5} {row.service:<24} "
                f"{row.box_size:<5} {row.gel_pack_count}gel  ${row.cost:>7.2f}  "
                f"arr {row.expected_arrival:%d %b} {row.predicted_arrival_temp_c:>6.2f}C "
                f"(margin {row.thermal_margin_c:.2f})"
            )

    if manifest.runners_up:
        lines.append("\n  runners-up")
        for other in manifest.runners_up:
            note = "" if other.covers_all else f"  strands {len(other.stranded)}"
            lines.append(
                f"    {'+'.join(other.carriers):<16} ${other.total_cost:>9,.2f}  "
                f"(+${other.extra_cost:,.2f}){note}"
            )

    for label, excluded in (("suppressed", manifest.suppressed), ("escalated", manifest.escalated)):
        if excluded:
            lines.append(f"\n  {label}")
            lines.extend(f"    {e.name:<22} {e.reason}" for e in excluded)
    for label, keys in (("stranded", manifest.stranded), ("infeasible", manifest.infeasible)):
        if keys:
            lines.append(f"\n  {label}: {', '.join(keys)}")

    return "\n".join(lines)
