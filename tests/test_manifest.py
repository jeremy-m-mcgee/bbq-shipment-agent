"""C6, against real recorded Shippo quotes."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from bbq_shipment_agent.planning import (
    Address,
    Excluded,
    Lane,
    RecordedQuoter,
    Shipment,
    assemble_manifest,
    render,
    solve_carriers,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shippo-quotes-sf-dc.json"
ORIGIN = Address("BBQ Kitchen", "64 Divisadero St", "San Francisco", "CA", "94117")
DEST = Address("Recipient One", "1600 Pennsylvania Ave NW", "Washington", "DC", "20500")

SATURDAY = date(2026, 8, 8)
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
ALL_DAYS = (SATURDAY, MONDAY, TUESDAY)


@pytest.fixture
def quoter():
    return RecordedQuoter.from_file(FIXTURE)


def shipments(count, **overrides):
    return tuple(
        Shipment(f"r{i}", f"Recipient {i}", address=DEST, **overrides)
        for i in range(count)
    )


def manifest_for(quoter, ships, dates=ALL_DAYS, **kwargs):
    return assemble_manifest("run-test", solve_carriers(ships, ORIGIN, dates, quoter), **kwargs)


class TestAssembly:
    def test_every_assignment_becomes_a_row(self, quoter):
        manifest = manifest_for(quoter, shipments(4))
        assert manifest.packet_count == 4
        assert {r.recipient_key for r in manifest.rows} == {f"r{i}" for i in range(4)}

    def test_rows_carry_the_fields_design_c6_lists(self, quoter):
        row = manifest_for(quoter, shipments(1)).rows[0]
        assert row.name and row.carrier and row.service
        assert row.box_size and row.gel_pack_count >= 0
        assert row.cost > 0
        assert row.address["zip"] == "20500"
        assert isinstance(row.expected_arrival, date)
        assert row.thermal_margin_c == pytest.approx(4.4 - row.predicted_arrival_temp_c, abs=0.01)

    def test_expected_arrival_is_ship_date_plus_transit(self, quoter):
        # Elapsed days, resolved per carrier: UPS quotes business days, USPS
        # calendar. See Configuration.elapsed_transit_days.
        manifest = manifest_for(quoter, shipments(1))
        row = manifest.rows[0]
        assert row.expected_arrival > row.ship_date
        assert (row.expected_arrival - row.ship_date) <= timedelta(days=7)

    def test_total_cost_is_the_sum_of_rows(self, quoter):
        manifest = manifest_for(quoter, shipments(3))
        assert manifest.total_cost == pytest.approx(sum(r.cost for r in manifest.rows))

    def test_it_refuses_to_publish_an_incomplete_plan(self, quoter):
        # A manifest built from a partial plan silently omits recipients while
        # looking complete. Design 4 routes that to C4 instead.
        hot = (Shipment("hot", "Hot", address=DEST, lane=Lane("furnace", 80.0)),)
        with pytest.raises(ValueError, match="no carrier subset covers"):
            manifest_for(quoter, hot)

    def test_the_capability_fingerprint_rides_along(self, quoter):
        manifest = manifest_for(quoter, shipments(1), cap_fingerprint="cap-abc123")
        assert manifest.cap_fingerprint == "cap-abc123"


class TestGroupedByShipDate:
    def test_it_groups_by_date(self, quoter):
        # Design 4: grouped by ship date "because that is the shape of the
        # physical work" -- the packer works one date at a time.
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )
        grouped = manifest_for(quoter, ships).by_ship_date()
        assert SATURDAY in grouped
        assert sum(len(rows) for rows in grouped.values()) == 3

    def test_dates_come_out_in_order(self, quoter):
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )
        dates = list(manifest_for(quoter, ships).by_ship_date())
        assert dates == sorted(dates)

    def test_packets_by_date_totals_the_run(self, quoter):
        manifest = manifest_for(quoter, shipments(5))
        assert sum(manifest.packets_by_date().values()) == manifest.packet_count


class TestRunnersUp:
    def test_they_are_attached_so_the_tradeoff_stays_visible(self, quoter):
        manifest = manifest_for(quoter, shipments(3))
        assert manifest.runners_up

    def test_extra_cost_is_the_premium_over_the_chosen_plan(self, quoter):
        manifest = manifest_for(quoter, shipments(3))
        for other in manifest.runners_up:
            assert other.extra_cost == pytest.approx(other.total_cost - manifest.total_cost)
            assert other.extra_cost >= 0

    def test_they_are_ranked_cheapest_first(self, quoter):
        manifest = manifest_for(quoter, shipments(3))
        costs = [o.total_cost for o in manifest.runners_up]
        assert costs == sorted(costs)

    def test_the_table_carries_its_own_denominator(self, quoter):
        """The runner-up table holds the subsets that *covered*, so without
        these a reader cannot tell whether the rest were worse, infeasible or
        never quoted. Same argument as `saturday_only`: a question the reader
        will ask needs a field to rest on."""
        manifest = manifest_for(quoter, shipments(3))
        assert manifest.quoting_carriers
        assert manifest.subsets_priced >= manifest.subsets_covering
        # At least the winner and its runners-up; more when a covering subset
        # was dropped for producing the winner's plan at the winner's cost.
        assert manifest.subsets_covering >= len(manifest.runners_up) + 1


class TestAccounting:
    def test_every_recipient_lands_in_exactly_one_bucket(self, quoter):
        # D1's first check. Providing the union here means that check compares
        # against a computed field rather than reassembling the set itself.
        manifest = manifest_for(
            quoter,
            shipments(3),
            suppressed=(Excluded("s1", "Suppressed", "duplicate of r0"),),
            escalated=(Excluded("e1", "Escalated", "address unresolved"),),
        )
        accounted = manifest.accounted_for()
        assert len(accounted) == len(set(accounted)) == 5

    def test_suppressed_and_escalated_survive_onto_the_manifest(self, quoter):
        manifest = manifest_for(
            quoter,
            shipments(1),
            suppressed=(Excluded("s1", "Suppressed", "duplicate of r0"),),
            escalated=(Excluded("e1", "Escalated", "address unresolved"),),
        )
        assert manifest.suppressed[0].reason == "duplicate of r0"
        assert manifest.escalated[0].name == "Escalated"


class TestSaturdayForcingSurfaces:
    def test_the_manifest_says_usps_was_forced(self, quoter):
        # Design 4, D2: stated as the reason rather than left implicit in the
        # ranking, because the operator needs to know why USPS is there.
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )
        manifest = manifest_for(quoter, ships, dates=(MONDAY, TUESDAY))
        assert manifest.forced_by_saturday
        assert "USPS" in manifest.carriers

    def test_the_manifest_names_who_forced_it(self, quoter):
        # `forced_by_saturday` alone says a constraint bound without saying
        # whose. Asked to explain the plan with only the boolean available,
        # `review-narrator` picked the wrong recipient and reasoned from it.
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )
        manifest = manifest_for(quoter, ships, dates=(MONDAY, TUESDAY))
        assert manifest.saturday_only == ("pinned",)

    def test_and_the_render_shows_it(self, quoter):
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )
        text = render(manifest_for(quoter, ships, dates=(MONDAY, TUESDAY)))
        assert "Saturday-only" in text and "pinned" in text


class TestRender:
    def test_it_shows_the_run_at_a_glance(self, quoter):
        manifest = manifest_for(quoter, shipments(3), cap_fingerprint="cap-abc123")
        text = render(manifest)
        assert manifest.run_id in text
        assert f"{manifest.total_cost:,.2f}" in text
        assert "cap-abc123" in text
        assert "runners-up" in text

    def test_every_row_appears(self, quoter):
        manifest = manifest_for(quoter, shipments(4))
        text = render(manifest)
        assert all(row.name in text for row in manifest.rows)

    def test_exclusions_are_shown_not_silently_dropped(self, quoter):
        manifest = manifest_for(
            quoter,
            shipments(1),
            suppressed=(Excluded("s1", "Barbara Liskov", "duplicate of r0"),),
            escalated=(Excluded("e1", "Edsger Dijkstra", "address unresolved"),),
        )
        text = render(manifest)
        assert "Barbara Liskov" in text and "Edsger Dijkstra" in text
