"""C2: enumerate configurations. Design section 4, Phase C.

"Cross product of box size, gel pack count, ship date, and carrier service,
per shipment. All four carriers at this stage. No pair restriction yet."

The pair restriction is still deliberately absent -- C5 evaluates carrier
subsets against this full set, and a shipment feasible under some carrier but
not the subset being scored is a *tradeoff*, not an infeasibility.

## What changed, and why the stage shape moved

The first version took a literal cross product against a declared service
table. That cannot work: which services exist depends on the account and the
lane, and is only discoverable by asking. So the cross product is now over the
part we control -- box size and gel pack count, which together define a
*parcel* -- and the carrier dimension comes back as an answer rather than
going in as an assumption.

One consequence is that C2 and C5's pricing step have collapsed into a single
call, because a quote carries the price and the transit estimate together.
Design 4 orders them separately; the API does not permit it.

Two filters still apply here, and neither is thermal:

* Saturday is USPS-only for perishables (design 3). A Saturday configuration
  on any other carrier is not a worse option, it is not an option.
* A configuration must physically exist -- the packet has to fit the box.

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
    ShipDay,
    elapsed_transit_days,
    ship_day_for,
)
from .load import Load
from .rates import (
    SATURDAY_CARRIERS,
    Address,
    CarrierMessage,
    ParcelSpec,
    Quote,
    RateQuoter,
    pin_carriers,
)


@dataclass(frozen=True)
class Configuration:
    """One candidate way to ship one packet, with its quote attached.

    The quote is part of the configuration rather than looked up later,
    because a service only exists in the answer that priced it. Cost and
    transit estimate therefore travel with the option that has them.
    """

    box_size: BoxSize
    gel_packs: int
    ship_date: date
    quote: Quote

    @property
    def carrier(self) -> str:
        return self.quote.carrier

    @property
    def service_name(self) -> str:
        return self.quote.service_name

    @property
    def cost(self) -> float:
        return self.quote.amount

    @property
    def transit_days(self) -> int | None:
        """Carrier estimate. `None` when the quote carried none, which C3
        treats as ungateable rather than guessing a number."""
        return self.quote.estimated_days

    @property
    def elapsed_transit_days(self) -> int | None:
        """Calendar days in transit, which is what C3 actually needs.

        `transit_days` is the carrier's own figure and the carriers disagree
        about what it counts -- UPS quotes business days, USPS calendar. This
        resolves that against the ship date, so a UPS two-day service leaving
        on a Saturday is three elapsed days rather than two.
        """
        if self.quote.estimated_days is None:
            return None
        return elapsed_transit_days(
            self.ship_date, self.quote.estimated_days, self.quote.business_days
        )

    @property
    def ship_day(self) -> ShipDay:
        return ship_day_for(self.ship_date)

    @property
    def box(self) -> Box:
        return BOXES[self.box_size]

    def describe(self) -> str:
        return (
            f"{self.ship_date.isoformat()} {self.carrier} {self.service_name} "
            f"{self.box_size.value}/{self.gel_packs}gel ${self.cost:.2f}"
        )


@dataclass(frozen=True)
class Enumeration:
    """C2's output for one shipment, plus what the quoting call revealed.

    `pinned_carriers` and `messages` are carried out of this stage rather than
    discarded: which carriers were available, and why any were not, is what
    makes a surprising plan explicable months later.
    """

    configurations: tuple[Configuration, ...]
    pinned_carriers: frozenset[str]
    messages: tuple[CarrierMessage, ...] = ()

    @property
    def carriers(self) -> frozenset[str]:
        return frozenset(c.carrier for c in self.configurations)


def smallest_fitting_box(load: Load) -> Box | None:
    """The smallest box the load physically fits in, or None if none does.

    Design 5's rule, applied rather than merely stated: "the smallest box that
    physically fits the load wins on cost and on thermal performance
    simultaneously". A larger box is dominated on both axes at this product
    weight -- more surface area and so a shorter hold time, plus higher
    dimensional weight -- and gains nothing back in ballast, because at 1.5 lb
    the ballast is not there.

    Measured on one live run before this became the rule: at the same lane and
    the same gel pack count, the small box was cheaper in 35 of 35 comparisons
    and the large box in none, while `TestBoxGeometry` pins the thermal half
    of the same claim. Both boxes also cap at `MAX_GEL_PACKS`, so a larger one
    cannot buy hold time a smaller one cannot.

    Ranked by outer volume, then tare. Both are physical facts about the box,
    so the choice does not move when the thermal model is re-tuned.
    """
    fitting = [box for box in BOXES.values() if load.fits_in(box)]
    if not fitting:
        return None
    return min(fitting, key=lambda box: (box.volume_cm3, box.tare_kg))


def parcel_variants(load: Load) -> tuple[ParcelSpec, ...]:
    """Every parcel the packet could physically be shipped in.

    The part of the configuration space we own outright. Gel pack count is
    crossed exhaustively because it is a genuine tradeoff -- more refrigerant
    is never thermally worse but always weighs more, and C5 wants the cheapest
    count that clears the gate. Box size is not crossed: `smallest_fitting_box`
    explains why the larger one can never win.

    This halves the quoting call count, which is the reason it was noticed --
    a live run spent 49 of 98 parcel quotes on a box that was dominated on
    every one of them. The correctness argument stands on its own, though, and
    would hold if quoting were free.
    """
    box = smallest_fitting_box(load)
    if box is None:
        return ()
    return tuple(
        ParcelSpec.build(load, box, gel_packs)
        for gel_packs in range(0, min(box.max_gel_packs, MAX_GEL_PACKS) + 1)
    )


def heaviest_variant(variants: tuple[ParcelSpec, ...]) -> ParcelSpec:
    """The parcel to pin the carrier set from.

    Largest volume, then heaviest. A carrier that will take the worst case
    will take the rest, whereas pinning off the smallest parcel can pin a
    carrier that later refuses a bigger box for a real reason -- turning a
    genuine restriction into a hard failure.
    """
    return max(
        variants,
        key=lambda p: (p.length_cm * p.width_cm * p.height_cm, p.weight_kg),
    )


def enumerate_configurations(
    load: Load,
    origin: Address,
    destination: Address,
    candidate_dates: tuple[date, ...],
    quoter: RateQuoter,
) -> Enumeration:
    """C2. Every real configuration for one shipment.

    `candidate_dates` are validated rather than filtered: `ship_day_for`
    raises on a date the operation does not ship, so a caller that passes a
    Wednesday finds out immediately instead of getting a silently smaller
    enumeration.

    Quotes are fetched once per parcel and reused across every candidate date.
    See `rates` for why that approximation is taken and when to revisit it.
    """
    if not candidate_dates:
        raise ValueError("no candidate ship dates; C2 has nothing to enumerate.")
    ship_days = {when: ship_day_for(when) for when in candidate_dates}

    variants = parcel_variants(load)
    if not variants:
        raise ValueError(
            f"the packet ({load.dimensions_m}) fits none of the available boxes."
        )

    pinned = pin_carriers(quoter, origin, destination, heaviest_variant(variants))

    configurations: list[Configuration] = []
    messages: list[CarrierMessage] = []
    for parcel in variants:
        result = quoter.quote(origin, destination, parcel, require=pinned)
        messages.extend(result.messages)
        for quote in result.quotes:
            for when, day in ship_days.items():
                if day is ShipDay.SATURDAY and quote.carrier not in SATURDAY_CARRIERS:
                    continue
                configurations.append(
                    Configuration(
                        box_size=parcel.box_size,
                        gel_packs=parcel.gel_packs,
                        ship_date=when,
                        quote=quote,
                    )
                )
    return Enumeration(
        configurations=tuple(configurations),
        pinned_carriers=pinned,
        messages=tuple(messages),
    )
