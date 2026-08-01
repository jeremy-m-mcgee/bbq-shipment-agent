"""C2: enumerate configurations. Design section 4, Phase C.

"Cross product of box size, gel pack count, ship date, and carrier service,
per shipment. All four carriers at this stage. No pair restriction yet."

The pair restriction is deliberately absent. C5 evaluates six carrier pairs
against this full set, and a shipment that is feasible under some carrier but
not the pair currently being scored is a *tradeoff*, not an infeasibility --
design 4 draws that line explicitly, because collapsing it is what turns a
stranded-shipment report into a mysteriously empty plan.

Two filters do apply here, and neither is thermal:

* Saturday is USPS-only for perishables (design 3). A Saturday FedEx
  configuration is not a worse option, it is not an option.
* A configuration must physically exist -- the packet has to fit the box and
  the gel packs have to fit beside it.

Everything thermal happens in C3, so this module never reads a temperature.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .catalog import (
    BOXES,
    MAX_GEL_PACKS,
    Box,
    BoxSize,
    Carrier,
    Service,
    ShipDay,
    services_for,
    ship_day_for,
)
from .load import Load


@dataclass(frozen=True)
class Configuration:
    """One candidate way to ship one packet.

    Frozen and hashable so C5 can use configurations as dictionary keys while
    scoring six pairs over the same enumeration without copying it.
    """

    box_size: BoxSize
    gel_packs: int
    ship_date: date
    service: Service

    @property
    def carrier(self) -> Carrier:
        return self.service.carrier

    @property
    def ship_day(self) -> ShipDay:
        return ship_day_for(self.ship_date)

    @property
    def box(self) -> Box:
        return BOXES[self.box_size]

    @property
    def transit_days(self) -> int:
        return self.service.transit_days

    def describe(self) -> str:
        """Compact identity for a manifest row or a finding."""
        return (
            f"{self.ship_date.isoformat()} {self.service.key} "
            f"{self.box_size.value}/{self.gel_packs}gel"
        )


def enumerate_configurations(
    load: Load, candidate_dates: tuple[date, ...]
) -> tuple[Configuration, ...]:
    """C2. Every physically real configuration for one shipment.

    `candidate_dates` are validated rather than filtered: `ship_day_for`
    raises on a date the operation does not ship, so a caller that passes a
    Wednesday finds out immediately instead of getting a silently smaller
    enumeration.
    """
    if not candidate_dates:
        raise ValueError("no candidate ship dates; C2 has nothing to enumerate.")

    configurations: list[Configuration] = []
    for when in candidate_dates:
        day = ship_day_for(when)
        for service in services_for(day):
            for box_size, box in BOXES.items():
                if not load.fits_in(box):
                    continue
                # Zero is a real candidate. It will not survive C3 for any
                # meaningful transit, but enumerating it keeps the gate the
                # only thing that decides feasibility.
                for gel_packs in range(0, min(box.max_gel_packs, MAX_GEL_PACKS) + 1):
                    configurations.append(
                        Configuration(
                            box_size=box_size,
                            gel_packs=gel_packs,
                            ship_date=when,
                            service=service,
                        )
                    )
    return tuple(configurations)
