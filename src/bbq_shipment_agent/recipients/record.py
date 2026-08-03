"""The record phase B works on, and where it stops.

Design 4 splits the pipeline at a real seam: phase B decides *who* is shipped
to, phase C decides *how*. `Recipient` is phase B's unit and `Shipment` is
phase C's, and the conversion happens once, at the end of B4.

## Why not just widen `Shipment`

B1 emits "a provenance pointer back to the source image and region, which B3
depends on" (design 4), and until now nothing carried it: `Shipment` is
`recipient_key, name, address, lane, required_ship_date` and `Excluded` is
three strings. The cheap fix was an optional field on `Shipment`.

That was rejected. `Shipment`'s own docstring calls it "the planning input:
one recipient, resolved and ready to plan against", and C1 through C6 would
have carried an image path and a confidence score they never read, through
every carrier subset and onto the manifest row. Provenance is evidence about
an *extraction*, not a property of a parcel, and the moment planning can see
it something will eventually use it.

So provenance lives here and dies at `to_shipment`. A stage that has no
business re-reading a screenshot cannot, because it is not holding one.

## The fields phase C never sees

`provenance` and `confidence` are both B1's output and both meaningless once
an address has been validated and repaired. `to_shipment` drops them, which
is the point rather than a limitation: the seam is enforced by the type, not
by a convention someone has to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..planning.rates import Address
from ..planning.shipment import Shipment
from ..planning.thermal import DEFAULT_LANE, Lane


@dataclass(frozen=True)
class Region:
    """Where in a screenshot something was read from.

    Pixels, top-left origin. Approximate is useful and exact is not required:
    the job is separating "the extractor pointed at the wrong place" from "it
    pointed at the right place and misread it", and a box that is a few pixels
    out still answers that.
    """

    x: int
    y: int
    width: int
    height: int

    def as_box(self) -> tuple[int, int, int, int]:
        """`(left, upper, right, lower)`, which is what croppers want."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)


@dataclass(frozen=True)
class Provenance:
    """Which image a record came from, and where in it.

    `region` is optional because a set of fixtures without regions is still
    usable — B3 falls back to re-reading the whole screenshot, less precisely.
    `source_image` is not optional: a record that cannot say where it came
    from cannot be re-read at all.
    """

    source_image: str
    region: Region | None = None


@dataclass(frozen=True)
class Recipient:
    """One person, as phase B sees them: an address plus how it was obtained.

    A hand-written roster produces these with no provenance and no confidence,
    which is correct — nothing extracted them, so there is nothing to point
    at and nothing to be unsure about. B1 fills both in.
    """

    key: str
    name: str
    address: Address
    lane: Lane = DEFAULT_LANE
    required_ship_date: date | None = None
    #: Where this came from. `None` for a hand-written roster.
    provenance: Provenance | None = None
    #: B1's confidence in the extraction, 0 to 1. `None` when not extracted.
    confidence: float | None = None

    def address_key(self) -> str:
        """Normalized address identity, for B4's duplicate check."""
        return self.address.cache_key()

    @property
    def extracted(self) -> bool:
        return self.provenance is not None

    def to_shipment(self) -> Shipment:
        """Hand phase C what it needs and nothing else. See the module docstring."""
        return Shipment(
            recipient_key=self.key,
            name=self.name,
            address=self.address,
            lane=self.lane,
            required_ship_date=self.required_ship_date,
        )


def to_shipments(recipients: tuple[Recipient, ...]) -> tuple[Shipment, ...]:
    """The B4 -> C1 boundary, in one place so it is greppable."""
    return tuple(r.to_shipment() for r in recipients)
