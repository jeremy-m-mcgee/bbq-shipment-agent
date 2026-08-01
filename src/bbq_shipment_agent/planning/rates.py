"""What a configuration costs. The seam Shippo eventually sits behind.

C5 assigns every shipment its cheapest feasible configuration, so it needs a
price for each one. Today that comes from a static table; at build order step
3's completion it comes from Shippo. `RateCard` is the seam, shaped like
`CapabilityProvider` and `ThermalModel`: narrow, injected, offline by default.

## Dimensional weight is the point

Carriers bill on whichever is greater, actual weight or dimensional weight.
At 1.5 lb of product the large box is nowhere near its dimensional weight in
actual mass, so it is billed on volume it is not using. That is the second
half of design 5's claim -- the larger box loses on cost *and* on thermal
performance simultaneously, with no compensating benefit, which is why its
selection is a signal worth investigating rather than a tradeoff.

## The numbers are invented

Base rates, per-kilogram rates, and the zone step are plausible placeholders,
not quotes. What should survive replacement by real Shippo rates is the
ordering -- overnight above two-day above ground, and the large box above the
small one for the same contents. A test asserts that ordering rather than any
figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .catalog import GEL_PACK_MASS_KG, Service
from .configurations import Configuration
from .load import Load
from .shipment import Shipment

#: Carriers bill volumetric weight as cm3 divided by a published divisor.
#: 5000 is the common international figure.
DIM_WEIGHT_DIVISOR = 5000.0

#: Rate zones run 1-8; each step adds this fraction to the line haul.
ZONE_STEP = 0.06


class RateUnavailable(Exception):
    """No price for this configuration. Not an error -- see `RateCard`."""


class RateCard(Protocol):
    """Prices one configuration for one shipment.

    Returns `None` rather than raising when a service cannot be priced, in the
    same spirit as C3 returning an empty feasible set: a service that does not
    serve a zone is an ordinary planning fact, and C5 simply does not consider
    it. Raising would make a routine gap look like a failure.
    """

    def quote(
        self, load: Load, configuration: Configuration, shipment: Shipment
    ) -> float | None: ...


def billable_weight_kg(load: Load, configuration: Configuration) -> float:
    """Greater of actual and dimensional weight -- what carriers bill on."""
    box = configuration.box
    actual = load.mass_kg + configuration.gel_packs * GEL_PACK_MASS_KG + box.tare_kg
    dimensional = box.volume_cm3 / DIM_WEIGHT_DIVISOR
    return max(actual, dimensional)


@dataclass(frozen=True)
class ServiceRate:
    """Base charge plus a per-kilogram line haul, before zone."""

    base: float
    per_kg: float


#: Placeholder rate table. Ordering is the load-bearing part, not the figures.
STATIC_RATES: dict[str, ServiceRate] = {
    "usps:priority_express": ServiceRate(base=28.00, per_kg=2.10),
    "usps:priority": ServiceRate(base=11.50, per_kg=1.35),
    "ups:next_day": ServiceRate(base=32.00, per_kg=2.40),
    "ups:second_day": ServiceRate(base=18.00, per_kg=1.70),
    "ups:ground": ServiceRate(base=9.00, per_kg=0.95),
    "fedex:overnight": ServiceRate(base=33.50, per_kg=2.45),
    "fedex:second_day": ServiceRate(base=17.50, per_kg=1.65),
    "fedex:ground": ServiceRate(base=8.75, per_kg=0.90),
    "dhl:express": ServiceRate(base=21.00, per_kg=1.90),
}


class StaticRateCard:
    """Prices from `STATIC_RATES`. Stands in until B2 brings Shippo online."""

    def __init__(self, rates: dict[str, ServiceRate] | None = None) -> None:
        self._rates = rates if rates is not None else STATIC_RATES

    def quote(
        self, load: Load, configuration: Configuration, shipment: Shipment
    ) -> float | None:
        rate = self._rates.get(configuration.service.key)
        if rate is None:
            return None
        weight = billable_weight_kg(load, configuration)
        zone_multiplier = 1.0 + ZONE_STEP * max(0, shipment.zone - 1)
        return round((rate.base + rate.per_kg * weight) * zone_multiplier, 2)


def cheapest_service(services: tuple[Service, ...]) -> Service | None:
    """Lowest base rate among the given services. Diagnostics only."""
    priced = [s for s in services if s.key in STATIC_RATES]
    if not priced:
        return None
    return min(priced, key=lambda s: STATIC_RATES[s.key].base)
