"""C5: solve carrier pairs. Design section 4, Phase C.

"Four carriers choose two is six candidate pairs. For each pair, assign every
shipment its cheapest feasible configuration by independent lookup. Brute
force over six options, not an optimization problem."

Independent lookup is the whole trick. Because each shipment's cheapest option
under a pair depends on nothing but that shipment, there is no interaction to
optimize over and no search: six pairs times twenty-two independent minimums.
Design 1 is explicit that no solver or heuristic is required anywhere in
planning, and this is the stage where that would otherwise be tempting.

## Stranded is not infeasible

A shipment feasible under some carrier but not under the pair being scored is
*stranded* -- a tradeoff this stage exists to surface, and design 4 says so
directly. A shipment feasible under no carrier at all is globally infeasible
and belongs to C4. Both appear on the result, separately, because conflating
them turns a reportable tradeoff into a mysteriously missing recipient.

## Ranking

Fully covering pairs rank by cost. Partially covering pairs are presented
separately rather than interleaved, so a cheap plan that quietly drops three
recipients can never outrank a complete one on a sorted list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import combinations

from .catalog import SATURDAY_CARRIERS, Carrier, ShipDay
from .configurations import enumerate_configurations
from .load import define_load
from .rates import RateCard, StaticRateCard
from .shipment import Shipment
from .thermal import EvaluatedConfiguration, ThermalModel, thermal_gate

#: Design 1: four carriers choose two. Sorted so the six pairs, and therefore
#: every ranking that ties on cost, come out in the same order every run.
CARRIER_PAIRS: tuple[tuple[Carrier, Carrier], ...] = tuple(
    combinations(sorted(Carrier), 2)
)


@dataclass(frozen=True)
class PricedConfiguration:
    """A configuration that passed C3 and has a quote."""

    evaluated: EvaluatedConfiguration
    cost: float

    @property
    def configuration(self):
        return self.evaluated.configuration

    @property
    def carrier(self) -> Carrier:
        return self.evaluated.configuration.carrier

    @property
    def thermal_margin_c(self) -> float:
        return self.evaluated.thermal_margin_c

    def _order(self) -> tuple[float, float, date, str, str, int]:
        """Deterministic cheapest-first ordering.

        Cost decides, then thermal margin (more headroom wins). Everything
        after that is a tie-break with no engineering meaning -- it exists so
        two runs over the same inputs never disagree, not because an earlier
        date or a lexically smaller service is better.

        Spelled out field by field rather than sorting on `describe()`,
        because deriving plan output from a display string means a change to
        the formatting silently reassigns ship dates across the whole run.
        """
        configuration = self.configuration
        return (
            self.cost,
            -self.thermal_margin_c,
            configuration.ship_date,
            configuration.service.key,
            configuration.box_size.value,
            configuration.gel_packs,
        )


@dataclass(frozen=True)
class Assignment:
    """One shipment's chosen configuration under one pair."""

    shipment: Shipment
    priced: PricedConfiguration

    @property
    def cost(self) -> float:
        return self.priced.cost


@dataclass(frozen=True)
class PairPlan:
    """What one carrier pair would do with the whole run."""

    carriers: tuple[Carrier, Carrier]
    assignments: tuple[Assignment, ...]
    #: Recipient keys feasible somewhere, but not under this pair.
    stranded: tuple[str, ...]
    #: True when this pair had to include a Saturday-capable carrier because
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
    def carriers_used(self) -> tuple[Carrier, ...]:
        """Carriers actually assigned work.

        Not the same as `carriers`: at most two per run is a ceiling, not a
        quota, so a pair can win while using only one of its members. The
        manifest should report what is actually being dropped off.
        """
        return tuple(sorted({a.priced.carrier for a in self.assignments}))

    @property
    def min_thermal_margin_c(self) -> float | None:
        if not self.assignments:
            return None
        return min(a.priced.thermal_margin_c for a in self.assignments)

    @property
    def ship_dates(self) -> tuple[date, ...]:
        return tuple(sorted({a.priced.configuration.ship_date for a in self.assignments}))

    def packets_by_date(self) -> dict[date, int]:
        """Per-date pack counts -- the shape of the physical work (design C6)."""
        counts: dict[date, int] = {}
        for assignment in self.assignments:
            when = assignment.priced.configuration.ship_date
            counts[when] = counts.get(when, 0) + 1
        return dict(sorted(counts.items()))


@dataclass(frozen=True)
class Solve:
    """The six-pair result, ranked."""

    #: Fully covering pairs, cheapest first.
    covering: tuple[PairPlan, ...]
    #: Partially covering pairs, most coverage first then cheapest. Kept apart
    #: from `covering` so a cheap plan that drops recipients cannot outrank a
    #: complete one.
    partial: tuple[PairPlan, ...]
    #: Recipient keys with no feasible configuration under any carrier. C4's
    #: input, not C5's problem.
    infeasible: tuple[str, ...]
    #: Recipient keys that can only ship on a Saturday.
    saturday_only: tuple[str, ...]

    @property
    def best(self) -> PairPlan | None:
        """The plan C6 builds a manifest from, or None if nothing covers."""
        return self.covering[0] if self.covering else None

    @property
    def runners_up(self) -> tuple[PairPlan, ...]:
        """Design C6 attaches these so the tradeoff stays visible."""
        return self.covering[1:]


def shipment_options(
    shipment: Shipment,
    candidate_dates: tuple[date, ...],
    rate_card: RateCard | None = None,
    model: ThermalModel | None = None,
) -> tuple[PricedConfiguration, ...]:
    """C1 through C3 plus pricing, for one shipment.

    A shipment pinned to a date by the operator is enumerated against that
    date alone -- see `Shipment.required_ship_date`. An unpriceable service is
    dropped rather than raising: a carrier that does not serve a zone is an
    ordinary planning fact.
    """
    card = rate_card or StaticRateCard()
    load = define_load(shipment.recipient_key)
    dates = (
        (shipment.required_ship_date,)
        if shipment.required_ship_date is not None
        else candidate_dates
    )
    feasible = thermal_gate(
        load, enumerate_configurations(load, dates), shipment.lane, model
    )

    priced: list[PricedConfiguration] = []
    for evaluated in feasible:
        cost = card.quote(load, evaluated.configuration, shipment)
        if cost is None:
            continue
        priced.append(PricedConfiguration(evaluated=evaluated, cost=cost))
    return tuple(priced)


def solve_pairs(
    shipments: tuple[Shipment, ...],
    candidate_dates: tuple[date, ...],
    rate_card: RateCard | None = None,
    model: ThermalModel | None = None,
) -> Solve:
    """C5. Score all six carrier pairs and rank them.

    Options are computed once per shipment and reused across all six pairs.
    Recomputing the thermal gate inside the pair loop would be six times the
    work for identical answers -- feasibility does not depend on which pair is
    being scored, only assignment does.
    """
    options = {s.recipient_key: shipment_options(s, candidate_dates, rate_card, model) for s in shipments}

    infeasible = tuple(s.recipient_key for s in shipments if not options[s.recipient_key])
    saturday_only = tuple(
        s.recipient_key
        for s in shipments
        if options[s.recipient_key]
        and all(
            p.configuration.ship_day is ShipDay.SATURDAY
            for p in options[s.recipient_key]
        )
    )

    plans: list[PairPlan] = []
    for pair in CARRIER_PAIRS:
        allowed = set(pair)
        assignments: list[Assignment] = []
        stranded: list[str] = []
        for shipment in shipments:
            available = [p for p in options[shipment.recipient_key] if p.carrier in allowed]
            if not available:
                # Infeasible-everywhere shipments belong to C4 and are
                # reported separately; they are not this pair's failure.
                if shipment.recipient_key not in infeasible:
                    stranded.append(shipment.recipient_key)
                continue
            assignments.append(
                Assignment(shipment=shipment, priced=min(available, key=lambda p: p._order()))
            )
        plans.append(
            PairPlan(
                carriers=pair,
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
    )
