"""C4: remediate global infeasibility. Design section 4, Phase C.

Runs only where C3 left a shipment with nothing feasible under any carrier.
Not the same thing as a stranded shipment, which is feasible somewhere but not
under the subset being scored — that is a tradeoff and belongs to C5.

## Why this is not an agent

Design 6.2 listed `infeasibility-remediation` among the four model-driven
loops, on the grounds that it "explores moves that C2 does not enumerate". It
had three moves: split the shipment, defer to the next run, or declare the
destination undeliverable.

Splitting was the open-ended one, and it turned out to be impossible. Hold
time is `latent_budget / leak_rate` and neither term contains the product
mass, so splitting leaves hold time identical while halving the only thermal
ballast in the box — measured at 36C over two days, 33.8C whole against 35.9C
halved. `TestSplittingDoesNotHelp` pins it.

What remained was two moves and a computation. Whether a cooler month rescues
a shipment is arithmetic once ambient varies by month, and declaring the
destination undeliverable is what is left when no month works. Design 2 says a
language model is not in the path of any decision that can be computed, and
design 6.3 exists to stop that boundary eroding, so C4 became ordinary Python
and the agent registry dropped to three.

## What it does not do

It proposes; it does not act. Nothing here edits the roster, defers anything,
or removes a recipient — the output is a recommendation with its reasoning
attached, for a human to accept at D2. That keeps the stage on the right side
of design 2 whether or not a model is involved.

It also never touches the 4.4C threshold. C4 is invoked *because* the gate
refused everything, which makes it the one place in the system where relaxing
the gate would look like a solution. The gate is a Python constant and the
only lever this module has is ambient, which is an input to the model rather
than a property of it.

## The month approximation, stated

Deferring is evaluated by re-running the thermal model over the same quoted
configurations under a later month's ambient. The quotes are not re-fetched
and the ship dates are not recomputed, so elapsed transit is taken from the
current run's candidate dates. Candidate dates are always a Saturday, Monday
and Tuesday, so the weekday alignment that decides elapsed days repeats every
month and the approximation is tight. Rates would differ in a real future run;
this is a feasibility answer, not a quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .configurations import enumerate_configurations
from .lanes import LaneBook
from .load import define_load
from .rates import Address, RateQuoter
from .shipment import Shipment
from .thermal import (
    MAX_ARRIVAL_TEMP_C,
    EvaluatedConfiguration,
    ThermalModel,
    evaluate_configurations,
)

#: How far ahead to look for a month that works. A year, because the answer
#: "next August" is still useful and anything beyond that is not a deferral,
#: it is an undeliverable with extra steps.
DEFAULT_HORIZON_MONTHS = 12


class RemediationMove(StrEnum):
    """What C4 recommends. Design 4, minus the move that cannot work."""

    DEFER = "defer"
    UNDELIVERABLE = "undeliverable"


@dataclass(frozen=True)
class Remediation:
    """One recommendation, with the numbers it rests on."""

    recipient_key: str
    name: str
    move: RemediationMove
    reason: str
    #: First month the shipment becomes feasible, for a deferral.
    earliest_month: date | None = None
    #: The ambient that month, and the one that defeated it now.
    deferred_ambient_c: float | None = None
    current_ambient_c: float | None = None
    #: How far short the best current option fell. Negative by construction --
    #: "two hours of hold time" and "a full day" call for different answers,
    #: and a filtered list cannot tell them apart.
    best_margin_c: float | None = None

    def describe(self) -> str:
        if self.move is RemediationMove.DEFER and self.earliest_month:
            return (
                f"{self.name}: defer to {self.earliest_month:%B %Y}. "
                f"{self.reason}"
            )
        return f"{self.name}: undeliverable. {self.reason}"


def _months_ahead(start: date, count: int) -> tuple[date, ...]:
    months = []
    year, month = start.year, start.month
    for _ in range(count):
        month += 1
        if month > 12:
            month, year = 1, year + 1
        months.append(date(year, month, 15))
    return tuple(months)


def _best(evaluated: tuple[EvaluatedConfiguration, ...]) -> EvaluatedConfiguration | None:
    """The option that came closest, feasible or not."""
    return max(evaluated, key=lambda e: e.thermal_margin_c, default=None)


def remediate(
    shipment: Shipment,
    origin: Address,
    candidate_dates: tuple[date, ...],
    quoter: RateQuoter,
    *,
    lane_book: LaneBook | None = None,
    model: ThermalModel | None = None,
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
) -> Remediation:
    """C4 for one shipment that nothing can currently carry.

    Without a `lane_book` there is no seasonal variation to search, so the
    only honest answer is undeliverable — and the reason says that the lack of
    lane data, not the destination, is what closed the search.
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
    evaluated = evaluate_configurations(
        load, enumeration.configurations, shipment.lane, model
    )
    if any(e.feasible for e in evaluated):
        # C4 runs on shipments C3 left with nothing, and `remediate_all`
        # filters on exactly that. Reaching here means the caller asked the
        # wrong question, and answering it would produce a recommendation to
        # defer a shipment that ships fine today.
        raise ValueError(
            f"{shipment.recipient_key} has feasible configurations; C4 handles "
            "global infeasibility only. A shipment feasible under some carrier "
            "but not the subset being scored is a tradeoff for C5."
        )
    now = _best(evaluated)
    best_margin = round(now.thermal_margin_c, 2) if now else None
    shortfall = (
        f"best option is {now.configuration.describe()} arriving "
        f"{now.predicted_arrival_temp_c:.1f}C, {abs(now.thermal_margin_c):.1f}C over "
        f"the {MAX_ARRIVAL_TEMP_C}C limit"
        if now
        else "no carrier quoted this destination at all"
    )

    if lane_book is None:
        return Remediation(
            recipient_key=shipment.recipient_key,
            name=shipment.name,
            move=RemediationMove.UNDELIVERABLE,
            reason=(
                f"{shortfall}. No lane configuration is loaded, so no cooler "
                "month could be checked — supply config/lanes.yaml to search "
                "for a deferral."
            ),
            current_ambient_c=shipment.lane.ambient_c,
            best_margin_c=best_margin,
        )

    state = shipment.address.state
    for month in _months_ahead(candidate_dates[0], horizon_months):
        lane = lane_book.lane_for(state, month)
        if lane.ambient_c >= shipment.lane.ambient_c:
            # No cooler than today; nothing to gain from asking.
            continue
        feasible = [
            e
            for e in evaluate_configurations(
                load, enumeration.configurations, lane, model
            )
            if e.feasible
        ]
        if feasible:
            cheapest = min(feasible, key=lambda e: e.configuration.cost)
            return Remediation(
                recipient_key=shipment.recipient_key,
                name=shipment.name,
                move=RemediationMove.DEFER,
                reason=(
                    f"{shortfall}. At {lane.ambient_c:.0f}C it clears: "
                    f"{cheapest.configuration.describe()} would arrive "
                    f"{cheapest.predicted_arrival_temp_c:.1f}C. Rates will differ "
                    "in a future run; this is a feasibility answer, not a quote."
                ),
                earliest_month=month,
                deferred_ambient_c=lane.ambient_c,
                current_ambient_c=shipment.lane.ambient_c,
                best_margin_c=best_margin,
            )

    return Remediation(
        recipient_key=shipment.recipient_key,
        name=shipment.name,
        move=RemediationMove.UNDELIVERABLE,
        reason=(
            f"{shortfall}. No month in the next {horizon_months} is cool enough "
            f"on this lane to change that, so the destination cannot be served "
            "with the current box and gel pack counts."
        ),
        current_ambient_c=shipment.lane.ambient_c,
        best_margin_c=best_margin,
    )


def remediate_all(
    shipments: tuple[Shipment, ...],
    infeasible: tuple[str, ...],
    origin: Address,
    candidate_dates: tuple[date, ...],
    quoter: RateQuoter,
    *,
    lane_book: LaneBook | None = None,
    model: ThermalModel | None = None,
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
) -> tuple[Remediation, ...]:
    """C4 across a run. Empty when C3 left nothing infeasible, which is usual."""
    keys = set(infeasible)
    return tuple(
        remediate(
            shipment,
            origin,
            candidate_dates,
            quoter,
            lane_book=lane_book,
            model=model,
            horizon_months=horizon_months,
        )
        for shipment in shipments
        if shipment.recipient_key in keys
    )
