"""The planning input: one recipient, resolved and ready to plan against.

Build order step 3 takes "a hand-written recipient list as input", so this is
what B2 and B4 will eventually produce and what C1 through C6 consume today.
Keeping it a plain frozen dataclass rather than a ledger record is deliberate:
planning runs entirely in memory and only the chosen plan is ever written, so
nothing here should be shaped by what the append-only file needs.

The address is a structured `Address` rather than a loose dict because it is
sent to a carrier, not just compared: a quote needs street, city, state and
zip in named fields, and B2's validation returns them that way.

## `required_ship_date`

Design 3 calls the Saturday interaction "the highest-leverage interaction in
the planning stage" -- one Saturday shipment forces USPS into the carrier set
and leaves exactly one free slot for the other twenty-one.

Nothing in C2 or C3 can produce that situation. Saturday offers a strict
subset of the weekday carriers (USPS only), and ship day does not enter the
thermal calculation at all, so a shipment feasible on Saturday is always also
feasible on Monday and Tuesday, and the solve would simply never choose
Saturday. The interaction can only arise from an operator pinning a shipment
to a date -- a recipient who is only home that weekend, a cook that finishes
Friday night.

So the field exists to make design 3's structural consequence expressible and
testable. It is optional and normally `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .rates import Address
from .thermal import DEFAULT_LANE, Lane


@dataclass(frozen=True)
class Shipment:
    """One recipient's shipment, as planning sees it."""

    recipient_key: str
    name: str
    address: Address
    #: Ambient assumption for this destination. Design 5 makes ambient a
    #: lane-based assumption, so it travels with the shipment rather than
    #: being a property of the run.
    lane: Lane = DEFAULT_LANE
    #: Operator pin. See the module docstring -- this is the only way a
    #: Saturday requirement can enter the plan.
    required_ship_date: date | None = None

    def destination(self) -> Address:
        return self.address

    def address_key(self) -> str:
        """Normalized address identity, for the duplicate check.

        B4 does the real consolidation; this is what D1 checks against and
        what stops the same doorstep being quoted twice.
        """
        return self.address.cache_key()
