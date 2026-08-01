"""C5: solve carrier subsets. Design section 4, Phase C.

"Four carriers choose two is six candidate pairs. For each pair, assign every
shipment its cheapest feasible configuration by independent lookup. Brute
force over six options, not an optimization problem."

Independent lookup is the whole trick. Because each shipment's cheapest option
under a subset depends on nothing but that shipment, there is no interaction
to optimize over and no search. Design 1 is explicit that no solver is
required anywhere in planning, and this is the stage where that would
otherwise be tempting.

## Subsets, not pairs

Design 4 says six pairs because it assumes four carriers. Both halves of that
turned out to be assumptions rather than facts: the available carrier set is
whatever quotes on the lane -- one on some runs, two on others -- and "at most
two per run" is a *ceiling*, not a quota, so a single carrier serving
everything is a legal and often winning answer.

So this enumerates every non-empty subset of size one to `MAX_CARRIERS_PER_RUN`
over the carriers that actually quoted. With four carriers that is ten subsets
rather than six pairs, and the four singletons are not redundant: one of them
routinely wins.

## Stranded is not infeasible

A shipment feasible under some carrier but not the subset being scored is
*stranded* -- a tradeoff this stage exists to surface, and design 4 says so
directly. A shipment feasible under no carrier at all is globally infeasible
and belongs to C4. Both appear on the result, separately, because conflating
them turns a reportable tradeoff into a mysteriously missing recipient.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import combinations

from .catalog import MAX_CARRIERS_PER_RUN, ShipDay
from .configurations import Enumeration, enumerate_configurations
from .load import define_load
from .rates import SATURDAY_CARRIERS, Address, CarrierMessage, RateQuoter
from .shipment import Shipment
from .thermal import EvaluatedConfiguration, ThermalModel, thermal_gate


def carrier_subsets(
    carriers: frozenset[str], max_size: int = MAX_CARRIERS_PER_RUN
) -> tuple[tuple[str, ...], ...]:
    """Every legal carrier combination, smallest first then alphabetical.

    Sorted so that two runs over the same carrier set enumerate in the same
    order, and so a tie on cost always resolves the same way.
    """
    ordered = sorted(carriers)
    subsets: list[tuple[str, ...]] = []
    for size in range(1, min(max_size, len(ordered)) + 1):
        subsets.extend(combinations(ordered, size))
    return tuple(subsets)


def _order(evaluated: EvaluatedConfiguration) -> tuple:
    """Deterministic cheapest-first ordering.

    Cost decides, then thermal margin (more headroom wins). Everything after
    is a tie-break with no engineering meaning -- it exists so two runs over
    the same inputs never disagree, not because an earlier date or a lexically
    smaller carrier is better. Spelled out field by field rather than sorting
    on a display string, so changing how a configuration prints cannot
    silently reassign ship dates across the run.
    """
    configuration = evaluated.configuration
    return (
        configuration.cost,
        -evaluated.thermal_margin_c,
        configuration.ship_date,
        configuration.carrier,
        configuration.service_name,
        configuration.box_size.value,
        configuration.gel_packs,
    )


@dataclass(frozen=True)
class Assignment:
    """One shipment's chosen configuration under one carrier subset."""

    shipment: Shipment
    evaluated: EvaluatedConfiguration

    @property
    def cost(self) -> float:
        return self.evaluated.configuration.cost

    @property
    def carrier(self) -> str:
        return self.evaluated.configuration.carrier


@dataclass(frozen=True)
class CarrierPlan:
    """What one carrier subset would do with the whole run."""

    carriers: tuple[str, ...]
    assignments: tuple[Assignment, ...]
    #: Recipient keys feasible somewhere, but not under this subset.
    stranded: tuple[str, ...]
    #: True when this subset had to include a Saturday-capable carrier because
    #: some shipment can only ship that day. Design 3 calls this the
    #: highest-leverage interaction in planning, so it is stated rather than
    #: left implicit in the ranking (design 4, D2).
    forced_by_saturday: bool

    @property
    def total_cost(self) -> float:
        return round(sum(a.cost for a in self.assignments), 2)

    @property
    def coverage(self) -> int:
        return len(self.assignments)

    @property
    def covers_all(self) -> bool:
        return not self.stranded

    @property
    def carriers_used(self) -> tuple[str, ...]:
        """Carriers actually assigned work.

        Not the same as `carriers`: at most two is a ceiling, not a quota, so
        a subset can win while using fewer than it contains. The manifest
        should report what is actually being dropped off.
        """
        return tuple(sorted({a.carrier for a in self.assignments}))

    @property
    def min_thermal_margin_c(self) -> float | None:
        if not self.assignments:
            return None
        return min(a.evaluated.thermal_margin_c for a in self.assignments)

    def packets_by_date(self) -> dict[date, int]:
        """Per-date pack counts -- the shape of the physical work (design C6)."""
        counts: dict[date, int] = {}
        for assignment in self.assignments:
            when = assignment.evaluated.configuration.ship_date
            counts[when] = counts.get(when, 0) + 1
        return dict(sorted(counts.items()))


@dataclass(frozen=True)
class Solve:
    """The ranked result over every legal carrier subset."""

    #: Fully covering subsets, cheapest first.
    covering: tuple[CarrierPlan, ...]
    #: Partially covering subsets, most coverage first then cheapest. Kept
    #: apart from `covering` so a cheap plan that drops recipients cannot
    #: outrank a complete one.
    partial: tuple[CarrierPlan, ...]
    #: Recipient keys with no feasible configuration under any carrier. C4's
    #: input, not C5's problem.
    infeasible: tuple[str, ...]
    #: Recipient keys that can only ship on a Saturday.
    saturday_only: tuple[str, ...]
    #: Carriers that quoted at all, across the run.
    available_carriers: frozenset[str]
    #: Why carriers did not quote, carried through for the run record.
    messages: tuple[CarrierMessage, ...] = ()

    @property
    def best(self) -> CarrierPlan | None:
        """The plan C6 builds a manifest from, or None if nothing covers."""
        return self.covering[0] if self.covering else None

    @property
    def runners_up(self) -> tuple[CarrierPlan, ...]:
        """Design C6 attaches these so the tradeoff stays visible."""
        return self.covering[1:]


def shipment_options(
    shipment: Shipment,
    origin: Address,
    candidate_dates: tuple[date, ...],
    quoter: RateQuoter,
    model: ThermalModel | None = None,
) -> tuple[tuple[EvaluatedConfiguration, ...], Enumeration]:
    """C1 through C3 for one shipment: what it can feasibly be shipped as.

    A shipment pinned to a date by the operator is enumerated against that
    date alone -- see `Shipment.required_ship_date`.
    """
    load = define_load(shipment.recipient_key)
    dates = (
        (shipment.required_ship_date,)
        if shipment.required_ship_date is not None
        else candidate_dates
    )
    enumeration = enumerate_configurations(
        load, origin, shipment.destination(), dates, quoter
    )
    feasible = thermal_gate(load, enumeration.configurations, shipment.lane, model)
    return feasible, enumeration


def solve_carriers(
    shipments: tuple[Shipment, ...],
    origin: Address,
    candidate_dates: tuple[date, ...],
    quoter: RateQuoter,
    model: ThermalModel | None = None,
) -> Solve:
    """C5. Score every legal carrier subset and rank them.

    Options are computed once per shipment and reused across every subset.
    Feasibility does not depend on which carriers are being scored, only
    assignment does, so gating inside the subset loop would be repeated work
    for identical answers.
    """
    options: dict[str, tuple[EvaluatedConfiguration, ...]] = {}
    messages: list[CarrierMessage] = []
    for shipment in shipments:
        feasible, enumeration = shipment_options(
            shipment, origin, candidate_dates, quoter, model
        )
        options[shipment.recipient_key] = feasible
        messages.extend(enumeration.messages)

    infeasible = tuple(s.recipient_key for s in shipments if not options[s.recipient_key])
    saturday_only = tuple(
        s.recipient_key
        for s in shipments
        if options[s.recipient_key]
        and all(
            e.configuration.ship_day is ShipDay.SATURDAY
            for e in options[s.recipient_key]
        )
    )
    available = frozenset(
        e.configuration.carrier for opts in options.values() for e in opts
    )

    plans: list[CarrierPlan] = []
    for subset in carrier_subsets(available):
        allowed = set(subset)
        assignments: list[Assignment] = []
        stranded: list[str] = []
        for shipment in shipments:
            candidates = [
                e
                for e in options[shipment.recipient_key]
                if e.configuration.carrier in allowed
            ]
            if not candidates:
                # Infeasible-everywhere shipments belong to C4 and are
                # reported separately; they are not this subset's failure.
                if shipment.recipient_key not in infeasible:
                    stranded.append(shipment.recipient_key)
                continue
            assignments.append(
                Assignment(shipment=shipment, evaluated=min(candidates, key=_order))
            )
        plans.append(
            CarrierPlan(
                carriers=subset,
                assignments=tuple(assignments),
                stranded=tuple(stranded),
                forced_by_saturday=bool(saturday_only)
                and bool(allowed & SATURDAY_CARRIERS),
            )
        )

    covering = tuple(
        sorted((p for p in plans if p.covers_all), key=lambda p: (p.total_cost, p.carriers))
    )
    partial = tuple(
        sorted(
            (p for p in plans if not p.covers_all),
            key=lambda p: (-p.coverage, p.total_cost, p.carriers),
        )
    )
    return Solve(
        covering=covering,
        partial=partial,
        infeasible=infeasible,
        saturday_only=saturday_only,
        available_carriers=available,
        messages=tuple(messages),
    )
