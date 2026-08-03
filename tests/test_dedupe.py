"""B4: dedupe and suppress."""

from datetime import date

from bbq_shipment_agent.planning import Address
from bbq_shipment_agent.recipients import Recipient, dedupe_recipients

SATURDAY = date(2026, 8, 15)
TUESDAY = date(2026, 8, 18)


def shipment(key, street, name=None, city="Austin", zip_="78701", pin=None):
    return Recipient(
        key=key,
        name=name or key,
        address=Address(name or key, street, city, "TX", zip_),
        required_ship_date=pin,
    )


class TestDuplicateKeys:
    def test_the_same_recipient_listed_twice_ships_once(self):
        ships = (shipment("r1", "1 Main St"), shipment("r1", "1 Main St"))
        report = dedupe_recipients(ships)
        assert [s.key for s in report.eligible] == ["r1"]
        assert report.duplicate_count == 1

    def test_the_first_occurrence_is_the_one_kept(self):
        # Input order decides, so a caller can put the authoritative row first.
        ships = (
            shipment("r1", "1 Main St", name="Ana"),
            shipment("r1", "1 Main St", name="Ana (dupe)"),
        )
        report = dedupe_recipients(ships)
        assert report.eligible[0].name == "Ana"

    def test_a_repeated_key_with_a_different_address_says_so(self):
        # B4 does not get to decide which row is right, but the reviewer
        # cannot decide either unless the conflict is on the manifest.
        ships = (shipment("r1", "1 Main St"), shipment("r1", "2 Other Ave"))
        report = dedupe_recipients(ships)
        assert "addresses differ" in report.suppressed[0].reason

    def test_distinct_keys_at_distinct_addresses_all_survive(self):
        ships = (shipment("r1", "1 Main St"), shipment("r2", "2 Other Ave"))
        report = dedupe_recipients(ships)
        assert len(report.eligible) == 2
        assert report.suppressed == ()


class TestSameAddressConsolidation:
    def test_two_people_at_one_doorstep_get_one_parcel(self):
        ships = (
            shipment("r1", "1 Main St", name="Ana"),
            shipment("r2", "1 Main St", name="Bo"),
        )
        report = dedupe_recipients(ships)
        assert [s.key for s in report.eligible] == ["r1"]
        assert report.consolidated == {"r1": ("r2",)}

    def test_the_suppression_reason_names_who_it_folded_onto(self):
        ships = (
            shipment("r1", "1 Main St", name="Ana"),
            shipment("r2", "1 Main St", name="Bo"),
        )
        report = dedupe_recipients(ships)
        assert "same address as Ana" in report.suppressed[0].reason

    def test_the_address_is_matched_case_insensitively(self):
        # A doorstep is not two doorsteps because the operator typed it in
        # lower case. Deeper normalization is B2's job: with validation on,
        # every address here is already the validator's canonical form.
        ships = (
            shipment("r1", "1 Main St", city="Austin"),
            shipment("r2", "1 main st", city="AUSTIN"),
        )
        report = dedupe_recipients(ships)
        assert len(report.eligible) == 1

    def test_a_third_at_the_same_address_folds_onto_the_same_shipment(self):
        ships = (
            shipment("r1", "1 Main St"),
            shipment("r2", "1 Main St"),
            shipment("r3", "1 Main St"),
        )
        report = dedupe_recipients(ships)
        assert report.consolidated == {"r1": ("r2", "r3")}
        assert report.consolidated_count == 2


class TestShipDatePins:
    def test_the_pin_survives_even_when_the_unpinned_row_is_listed_first(self):
        # Design 3: the Saturday pin is the highest-leverage interaction in
        # planning. Folding the pinned shipment onto the unpinned one because
        # it happened to be listed second would quietly change the carrier
        # set, which is why consolidation groups before it decides.
        ships = (
            shipment("r1", "1 Main St"),
            shipment("r2", "1 Main St", pin=SATURDAY),
        )
        report = dedupe_recipients(ships)
        assert [s.key for s in report.eligible] == ["r2"]
        assert report.eligible[0].required_ship_date == SATURDAY

    def test_list_order_does_not_change_the_outcome(self):
        forward = dedupe_recipients(
            (shipment("r1", "1 Main St"), shipment("r2", "1 Main St", pin=SATURDAY))
        )
        reverse = dedupe_recipients(
            (shipment("r2", "1 Main St", pin=SATURDAY), shipment("r1", "1 Main St"))
        )
        assert forward.eligible == reverse.eligible

    def test_two_shipments_pinned_to_the_same_date_consolidate(self):
        ships = (
            shipment("r1", "1 Main St", pin=SATURDAY),
            shipment("r2", "1 Main St", pin=SATURDAY),
        )
        report = dedupe_recipients(ships)
        assert [s.key for s in report.eligible] == ["r1"]
        assert report.eligible[0].required_ship_date == SATURDAY

    def test_two_pinned_dates_at_one_address_are_two_deliveries(self):
        ships = (
            shipment("r1", "1 Main St", pin=SATURDAY),
            shipment("r2", "1 Main St", pin=TUESDAY),
        )
        report = dedupe_recipients(ships)
        assert len(report.eligible) == 2
        assert report.suppressed == ()

    def test_an_unpinned_shipment_joins_the_only_pin_at_its_address(self):
        # "Any date" is satisfied by that date, so one parcel serves both.
        ships = (
            shipment("r1", "1 Main St", name="Ana", pin=SATURDAY),
            shipment("r2", "1 Main St", name="Bo"),
        )
        report = dedupe_recipients(ships)
        assert [s.key for s in report.eligible] == ["r1"]
        assert report.eligible[0].required_ship_date == SATURDAY

    def test_unpinned_shipments_do_not_guess_between_two_pins(self):
        ships = (
            shipment("r1", "1 Main St", pin=SATURDAY),
            shipment("r2", "1 Main St", pin=TUESDAY),
            shipment("r3", "1 Main St"),
            shipment("r4", "1 Main St"),
        )
        report = dedupe_recipients(ships)
        # The two pins keep their parcels, and the unpinned pair consolidates
        # with each other rather than joining either date arbitrarily.
        assert [s.key for s in report.eligible] == ["r1", "r2", "r3"]
        assert report.consolidated == {"r3": ("r4",)}


class TestReport:
    def test_nobody_is_silently_dropped(self):
        ships = (
            shipment("r1", "1 Main St"),
            shipment("r1", "1 Main St"),
            shipment("r2", "1 Main St"),
            shipment("r3", "9 Far Rd"),
        )
        report = dedupe_recipients(ships)
        accounted = [s.key for s in report.eligible] + [
            e.recipient_key for e in report.suppressed
        ]
        assert sorted(accounted) == ["r1", "r1", "r2", "r3"]

    def test_every_suppression_carries_a_reason(self):
        ships = (shipment("r1", "1 Main St"), shipment("r2", "1 Main St"))
        report = dedupe_recipients(ships)
        assert all(e.reason for e in report.suppressed)

    def test_an_empty_list_is_not_an_error(self):
        report = dedupe_recipients(())
        assert report.eligible == () and report.suppressed == ()

    def test_it_reads_no_prior_state(self):
        # Design 9 dropped time-based suppression, which was the only thing
        # in the spine that consulted the ledger. Running the same list twice
        # has to give the same answer, with no clock and no history in play.
        ships = (
            shipment("r1", "1 Main St"),
            shipment("r2", "1 Main St"),
            shipment("r3", "9 Far Rd", pin=SATURDAY),
        )
        first = dedupe_recipients(ships)
        second = dedupe_recipients(ships)
        assert first == second
