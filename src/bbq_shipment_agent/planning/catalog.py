"""The physical and carrier domain. Design sections 3 and 5.

Everything here is small and closed by construction: two box sizes, seven gel
pack counts, three ship days, four carriers. That is the property section 1
relies on when it says the configuration space is "small enough to enumerate
exhaustively" and needs no solver.

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
from datetime import date
from enum import StrEnum

LB_PER_KG = 2.20462


class BoxSize(StrEnum):
    SMALL = "small"
    LARGE = "large"


class Carrier(StrEnum):
    """The four carriers of design 3. At most two may be used in one run."""

    USPS = "usps"
    UPS = "ups"
    FEDEX = "fedex"
    DHL = "dhl"


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
_WALL_M = 0.05

#: Design 3: two box sizes. Design 5 is emphatic that at 1.5 lb the larger one
#: is strictly worse -- more surface area, shorter hold time, higher
#: dimensional weight, and no compensating thermal mass because the mass is
#: not there. It is enumerated for completeness and its selection is a signal
#: worth investigating, which is why D1 checks for it.
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
GEL_PACK_MASS_KG = 0.5
#: Latent heat of fusion, water-based gel, J/kg. This is the whole cooling
#: budget: design 5 puts gel packs at roughly 77 percent of it at this product
#: weight, with the product itself contributing almost no thermal ballast.
GEL_PACK_LATENT_HEAT_J_KG = 334_000
#: Gel packs hold at their melting point while any solid fraction remains.
GEL_PACK_PHASE_TEMP_C = 0.0


@dataclass(frozen=True)
class Service:
    """One carrier service level, with the transit time C3 gates on."""

    carrier: Carrier
    code: str
    transit_days: int

    @property
    def key(self) -> str:
        return f"{self.carrier.value}:{self.code}"


#: Design 1: four carriers available. Transit days are the carrier's estimate,
#: which design 5 lists as the one externally-supplied and variable thermal
#: input -- everything else is known or computed.
SERVICES: tuple[Service, ...] = (
    Service(Carrier.USPS, "priority_express", 1),
    Service(Carrier.USPS, "priority", 2),
    Service(Carrier.UPS, "next_day", 1),
    Service(Carrier.UPS, "second_day", 2),
    Service(Carrier.UPS, "ground", 3),
    Service(Carrier.FEDEX, "overnight", 1),
    Service(Carrier.FEDEX, "second_day", 2),
    Service(Carrier.FEDEX, "ground", 3),
    Service(Carrier.DHL, "express", 2),
)

SERVICES_BY_KEY: dict[str, Service] = {s.key: s for s in SERVICES}

#: Design 3: Saturday shipments are USPS only for perishables, given weekend
#: ground schedules. A soft constraint in the sense that it comes from carrier
#: operations rather than food safety, but a hard filter on what may be
#: enumerated -- a Saturday FedEx configuration is not a tradeoff, it is not a
#: real option.
SATURDAY_CARRIERS: frozenset[Carrier] = frozenset({Carrier.USPS})


def services_for(day: ShipDay) -> tuple[Service, ...]:
    """The services actually available on a given ship day."""
    if day is ShipDay.SATURDAY:
        return tuple(s for s in SERVICES if s.carrier in SATURDAY_CARRIERS)
    return SERVICES
