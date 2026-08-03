"""The physical and carrier domain. Design sections 3 and 5.

What lives here is the part of the domain we own: two box sizes, seven gel
pack counts, three ship days. That is still small enough to enumerate
exhaustively, which is what section 1 relies on when it says no solver is
needed.

Carriers and services are deliberately *not* here. They were, as a four-member
enum and a nine-entry service table with transit times chosen by hand, and
against the live API that was wrong in kind: DHL will not quote US domestic,
UPS depends on the origin, FedEx had no account, and USPS offered a service the
enum did not contain. Which carriers exist is a property of the account and the
lane, discovered at quote time -- see `rates`.

## Units

SI throughout -- kilograms, metres, seconds, degrees Celsius. The design doc
quotes product weight in pounds because that is how the packets are described,
but mixing unit systems inside a thermal calculation is the classic way to
lose a spacecraft, so the conversion happens once, here, and never again.

## What is an assumption

Box geometry, gel pack mass, and the EPS conductivity below are nominal values
chosen to be physically plausible. Design 5 requires assumptions to be stated
rather than buried: these are stated, and they are the numbers step 4's
calibrated model replaces. The *structure* -- that hold time falls as surface
area rises, that gel packs carry the cooling budget -- is not an assumption
and should survive recalibration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

LB_PER_KG = 2.20462


class BoxSize(StrEnum):
    SMALL = "small"
    LARGE = "large"


#: Design 3: at most two carriers per run, for operational simplicity at
#: drop-off. Enforced in code (C5), never configurable.
MAX_CARRIERS_PER_RUN = 2


class ShipDay(StrEnum):
    """Design 3, soft constraints. Ship days are Saturday, Monday, Tuesday."""

    SATURDAY = "saturday"
    MONDAY = "monday"
    TUESDAY = "tuesday"


#: `date.weekday()` -> ShipDay, for the days that are shippable at all.
_WEEKDAY_TO_SHIP_DAY: dict[int, ShipDay] = {
    0: ShipDay.MONDAY,
    1: ShipDay.TUESDAY,
    5: ShipDay.SATURDAY,
}


class ShipDayError(ValueError):
    """A candidate date is not a day this operation ships on."""


def ship_day_for(when: date) -> ShipDay:
    """Classify a calendar date, refusing days the operation does not ship.

    Rejecting rather than silently skipping: a caller that passes Wednesday
    has misunderstood something, and a quietly empty configuration set is a
    much worse way to find that out than an exception naming the date.
    """
    try:
        return _WEEKDAY_TO_SHIP_DAY[when.weekday()]
    except KeyError:
        raise ShipDayError(
            f"{when.isoformat()} is a {when.strftime('%A')}; this operation "
            f"ships {', '.join(d.value for d in ShipDay)} only."
        ) from None


@dataclass(frozen=True)
class Box:
    """One box size, with the geometry the thermal model needs.

    Carries dimensions and wall thickness rather than a precomputed UA value.
    Design 5 computes UA from geometry and material properties, so the box has
    to expose the geometry for step 4 to compute against; a stored UA would
    make the box a thermal-model output rather than a physical fact.
    """

    size: BoxSize
    #: Interior cavity, metres. The load has to fit inside this.
    inner_m: tuple[float, float, float]
    #: Insulation thickness, metres.
    wall_m: float
    material: str
    #: Insulation conductivity, W/(m*K).
    conductivity_w_mk: float
    max_gel_packs: int
    #: Empty box mass, kg. Part of billable weight, not of the thermal model:
    #: the walls are treated as pure resistance, not as ballast.
    tare_kg: float

    @property
    def volume_cm3(self) -> float:
        """Outer volume, for dimensional weight. Design 5 notes the larger
        box costs more here as well as thermally."""
        length, width, height = self.outer_m
        return length * width * height * 1_000_000

    @property
    def outer_m(self) -> tuple[float, float, float]:
        return tuple(d + 2 * self.wall_m for d in self.inner_m)  # type: ignore[return-value]

    @property
    def surface_area_m2(self) -> float:
        """Outer surface area -- the area heat actually leaks across."""
        length, width, height = self.outer_m
        return 2 * (length * width + length * height + width * height)

    @property
    def ua_w_k(self) -> float:
        """Conductance through the walls, W/K. Design 5's computed UA.

        Plane-wall approximation: conduction only, ignoring corners, seams,
        and the internal and external film coefficients. Adequate for ranking
        two box sizes against each other, which is all the spine asks of it.
        """
        return self.conductivity_w_mk * self.surface_area_m2 / self.wall_m


#: Expanded polystyrene, the usual insulated-shipper material.
_EPS_CONDUCTIVITY_W_MK = 0.033
#: 5cm EPS wall. Thickening this is NOT the lever it looks like: at a fixed
#: inner cavity, a thicker wall grows the outer surface area almost as fast as
#: the thickness, so UA barely moves. Measured, 5cm -> 7cm changed UA from
#: 0.2475 to 0.2379 W/K -- a 4% gain -- while raising dimensional weight
#: enough to make every run more expensive. Refrigerant mass and insulation
#: conductivity are the levers; thickness is not.
_WALL_M = 0.05

#: Design 3: two box sizes. Design 5 is emphatic that at 1.5 lb the larger one
#: is strictly worse -- more surface area, shorter hold time, higher
#: dimensional weight, and no compensating thermal mass because the mass is
#: not there.
#:
#: Both are defined; only the smallest one the load fits in is quoted. This
#: entry used to say the larger was "enumerated for completeness and its
#: selection is a signal worth investigating", which could not happen: being
#: dominated on both cost and thermal margin, it can never be selected, so the
#: signal can never fire. It also cannot rescue an infeasible shipment, since
#: it is never thermally better and caps at the same `MAX_GEL_PACKS`. See
#: `configurations.smallest_fitting_box`. It stays defined because the rule is
#: "smallest box that fits", which needs something to be larger than.
BOXES: dict[BoxSize, Box] = {
    BoxSize.SMALL: Box(
        size=BoxSize.SMALL,
        inner_m=(0.15, 0.15, 0.15),
        wall_m=_WALL_M,
        material="EPS",
        conductivity_w_mk=_EPS_CONDUCTIVITY_W_MK,
        max_gel_packs=6,
        tare_kg=0.5,
    ),
    BoxSize.LARGE: Box(
        size=BoxSize.LARGE,
        inner_m=(0.25, 0.25, 0.25),
        wall_m=_WALL_M,
        material="EPS",
        conductivity_w_mk=_EPS_CONDUCTIVITY_W_MK,
        max_gel_packs=6,
        tare_kg=0.9,
    ),
}

#: Design 3: gel packs, not dry ice. Avoids hazmat classification and keeps
#: all four carriers available.
MAX_GEL_PACKS = 6
GEL_PACK_MASS_KG = 0.7
#: Latent heat of fusion, water-based gel, J/kg. This is the whole cooling
#: budget: design 5 puts gel packs at roughly 77 percent of it at this product
#: weight, with the product itself contributing almost no thermal ballast.
GEL_PACK_LATENT_HEAT_J_KG = 334_000
#: Gel packs hold at their melting point while any solid fraction remains.
GEL_PACK_PHASE_TEMP_C = 0.0


def add_business_days(start: date, count: int) -> date:
    """Move `count` business days forward from `start`, skipping weekends."""
    when = start
    moved = 0
    while moved < count:
        when += timedelta(days=1)
        if when.weekday() < 5:
            moved += 1
    return when


def elapsed_transit_days(ship_date: date, estimated_days: int, business_days: bool) -> int:
    """Calendar days a shipment is actually in transit.

    Carriers do not agree on what they are quoting. UPS says "second business
    day"; USPS says "delivery in 2 to 5 days". Treating both as calendar days
    understates UPS transit whenever the journey crosses a weekend -- and C3
    gates on this figure as elapsed time, so understating it means the gel
    packs are asked to last longer than the model thinks.

    Saturday delivery is not modelled. Some services offer it, which would
    make this an overestimate for those; erring long is the safe direction for
    a food-safety gate.
    """
    if not business_days:
        return estimated_days
    return (add_business_days(ship_date, estimated_days) - ship_date).days
