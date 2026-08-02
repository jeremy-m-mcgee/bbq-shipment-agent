"""B4: dedupe and suppress. Design section 4, Phase B.

"Within-run duplicates, then same-address consolidation. No cross-run check --
see section 9."

Two passes, in that order, because they answer different questions. The first
catches the same recipient key listed twice: an operator scrolling a list and
pasting a row again. The second catches two *different* recipients resolving
to the same doorstep: a couple who both asked, a housemate, a work address.
Only the second is a judgement call, and design 4 settles it -- one parcel per
doorstep, with the people it covers named on the suppression entry.

## Pure function of its input

Design 4 marks B4 deterministic and design 9 explains what that cost: the
original design excluded anyone served inside a configurable window, checked
against the ledger. Dropping that removed the only prior-state dependency in
the deterministic spine, so this module reads nothing -- no ledger, no clock,
no config. The same recipient list always produces the same eligible set, and
a surprising suppression is explained entirely by the list in front of you.

## Why the ship-date pin survives consolidation

Design 3 calls the Saturday interaction "the highest-leverage interaction in
the planning stage", and `Shipment.required_ship_date` is the only way it can
arise. Folding a pinned shipment onto an unpinned one at the same address
would silently discard that pin and change which carriers the run can use, so
consolidation groups by address *and* pin: same doorstep, same date is one
parcel; same doorstep on two different dates is two deliveries the operator
asked for. Unpinned shipments fold onto a pinned one where there is exactly
one pin to fold onto, since "any date" is satisfied by every date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..planning.manifest import Excluded
from ..planning.shipment import Shipment


@dataclass(frozen=True)
class SuppressionReport:
    """B4's output: who ships, who does not, and why."""

    eligible: tuple[Shipment, ...]
    suppressed: tuple[Excluded, ...]
    #: Kept recipient key -> the keys consolidated onto it. Empty for a run
    #: with no shared addresses. Carried separately from `suppressed` because
    #: the packer needs it the other way round: this parcel covers these
    #: people, so a card with two names goes in the box.
    consolidated: dict[str, tuple[str, ...]]

    @property
    def duplicate_count(self) -> int:
        return sum(1 for e in self.suppressed if e.reason.startswith("duplicate"))

    @property
    def consolidated_count(self) -> int:
        return sum(len(keys) for keys in self.consolidated.values())


def dedupe_shipments(shipments: tuple[Shipment, ...]) -> SuppressionReport:
    """B4. Collapse the recipient list to one shipment per doorstep.

    Input order is preserved and decides which shipment is kept, so a caller
    can put the authoritative row first. Everyone excluded appears in
    `suppressed` with a reason naming the shipment they were folded onto --
    design 4 requires an explicit reason for every exclusion, and "duplicate"
    on its own does not tell the operator which row survived.
    """
    kept, suppressed = _drop_duplicate_keys(shipments)
    return _consolidate_addresses(kept, suppressed)


def _drop_duplicate_keys(
    shipments: tuple[Shipment, ...],
) -> tuple[list[Shipment], list[Excluded]]:
    """Pass one: the same recipient key listed more than once."""
    kept: list[Shipment] = []
    suppressed: list[Excluded] = []
    seen: dict[str, Shipment] = {}

    for shipment in shipments:
        first = seen.get(shipment.recipient_key)
        if first is None:
            seen[shipment.recipient_key] = shipment
            kept.append(shipment)
            continue

        # A repeated key with a *different* address is an operator error
        # either way, and which row is right is not B4's to decide. Keeping
        # the first and saying so in the reason puts the conflict in front of
        # the reviewer rather than resolving it silently.
        conflict = (
            ""
            if first.address_key() == shipment.address_key()
            else f"; addresses differ ({shipment.address.street1!r} vs "
            f"{first.address.street1!r}) -- check which is right"
        )
        suppressed.append(
            Excluded(
                recipient_key=shipment.recipient_key,
                name=shipment.name,
                reason=f"duplicate of {first.name} already in this run{conflict}",
            )
        )

    return kept, suppressed


def _consolidate_addresses(
    shipments: list[Shipment], suppressed: list[Excluded]
) -> SuppressionReport:
    """Pass two: distinct recipients resolving to the same doorstep.

    Grouped rather than streamed. Deciding as each shipment arrives would make
    the answer depend on list order in a way the operator cannot see: an
    unpinned row listed before a Saturday-pinned one at the same address would
    take its own parcel, and listed after it would fold in. The whole group is
    in hand before any of it is resolved.
    """
    groups: dict[str, list[Shipment]] = {}
    for shipment in shipments:
        groups.setdefault(shipment.address_key(), []).append(shipment)

    holder_of: dict[str, Shipment] = {}
    for group in groups.values():
        holder_of.update(_assign_holders(group))

    consolidated: dict[str, list[str]] = {}
    eligible: list[Shipment] = []

    for shipment in shipments:
        holder = holder_of[shipment.recipient_key]
        if holder.recipient_key == shipment.recipient_key:
            eligible.append(shipment)
            continue

        consolidated.setdefault(holder.recipient_key, []).append(shipment.recipient_key)
        suppressed.append(
            Excluded(
                recipient_key=shipment.recipient_key,
                name=shipment.name,
                reason=(
                    f"same address as {holder.name}; consolidated onto that "
                    f"shipment ({shipment.address.street1}, {shipment.address.city})"
                ),
            )
        )

    return SuppressionReport(
        eligible=tuple(eligible),
        suppressed=tuple(suppressed),
        consolidated={k: tuple(v) for k, v in consolidated.items()},
    )


def _assign_holders(group: list[Shipment]) -> dict[str, Shipment]:
    """Which shipment each member of one address group ships under.

    A shipment that holds its own slot maps to itself. See the module
    docstring for why the pin decides: every distinct pinned date at an
    address is a real, separate delivery, so each keeps a parcel. Unpinned
    shipments fold onto the pin when there is exactly one -- "any date" is
    satisfied by that date -- and otherwise fold together onto the first of
    their own kind, since joining one of two pinned dates would be a guess.
    """
    by_pin: dict[date | None, Shipment] = {}
    # Pinned shipments claim their slot first. If an unpinned one could claim
    # it, a list that happened to put the unpinned row first would leave the
    # pinned shipment folded onto a shipment with no pin, which is precisely
    # the silent loss of a Saturday requirement this is guarding against.
    for shipment in group:
        if shipment.required_ship_date is not None:
            by_pin.setdefault(shipment.required_ship_date, shipment)

    pins = list(by_pin)

    def slot(shipment: Shipment) -> date | None:
        if shipment.required_ship_date is not None:
            return shipment.required_ship_date
        return pins[0] if len(pins) == 1 else None

    for shipment in group:
        if shipment.required_ship_date is None:
            by_pin.setdefault(slot(shipment), shipment)

    return {s.recipient_key: by_pin[slot(s)] for s in group}
