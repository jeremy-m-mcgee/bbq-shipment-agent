"""Ambient assumptions per destination. Design 5, and design 10's open question."""

from datetime import date

import pytest

from bbq_shipment_agent.planning import DEFAULT_LANE, LaneBook, LaneBookError

AUGUST = date(2026, 8, 15)
JANUARY = date(2026, 1, 10)

BOOK = {
    "default_band": "hot",
    "bands": {"hot": 32.0, "temperate": 22.0, "cool": 16.0},
    "states": {"TX": "hot", "NY": "temperate", "MN": "cool"},
    "seasonal_offset_c": {1: -8, 8: 4},
}


@pytest.fixture
def book():
    return LaneBook.from_mapping(BOOK)


class TestBands:
    def test_a_mapped_state_takes_its_band(self, book):
        assert book.lane_for("MN").ambient_c == 16.0

    def test_the_state_code_is_case_insensitive(self, book):
        assert book.lane_for("mn").ambient_c == book.lane_for("MN").ambient_c

    def test_an_unmapped_state_takes_the_conservative_default(self, book):
        # Not the average band. An unstated assumption must not be the
        # optimistic one when a food safety gate depends on it -- the same
        # reasoning that leaves Saturday delivery unmodelled.
        assert book.lane_for("ZZ").ambient_c == book.bands["hot"]
        assert book.lane_for("ZZ").ambient_c == max(book.bands.values())

    def test_an_unmapped_state_says_so_in_the_key(self, book):
        # The key travels onto the manifest and into the ledger, so a plan
        # priced off a guess is distinguishable from one priced off a belief.
        assert "unmapped" in book.lane_for("ZZ").key
        assert "unmapped" not in book.lane_for("TX").key


class TestSeason:
    def test_the_offset_moves_with_the_ship_month(self, book):
        assert book.lane_for("NY", AUGUST).ambient_c == 26.0
        assert book.lane_for("NY", JANUARY).ambient_c == 14.0

    def test_a_month_with_no_offset_takes_the_band_unchanged(self, book):
        assert book.lane_for("NY", date(2026, 5, 1)).ambient_c == 22.0

    def test_no_date_means_no_offset(self, book):
        assert book.lane_for("NY").ambient_c == 22.0

    def test_the_month_is_named_in_the_key(self, book):
        assert "Aug" in book.lane_for("NY", AUGUST).key


class TestRefusals:
    def test_a_default_band_that_is_not_a_band_is_refused(self):
        with pytest.raises(LaneBookError, match="default_band"):
            LaneBook.from_mapping({**BOOK, "default_band": "balmy"})

    def test_a_state_mapped_to_an_unknown_band_is_refused(self):
        with pytest.raises(LaneBookError, match="unknown band"):
            LaneBook.from_mapping({**BOOK, "states": {"TX": "scorching"}})

    def test_a_non_numeric_band_is_refused(self):
        with pytest.raises(LaneBookError, match="degrees C"):
            LaneBook.from_mapping({**BOOK, "bands": {"hot": "very"}})

    def test_no_bands_is_refused(self):
        with pytest.raises(LaneBookError, match="non-empty"):
            LaneBook.from_mapping({"bands": {}, "default_band": "hot"})

    def test_a_month_outside_the_year_is_refused(self):
        with pytest.raises(LaneBookError, match="not 1-12"):
            LaneBook.from_mapping({**BOOK, "seasonal_offset_c": {13: 1}})

    def test_a_missing_file_reports_the_path(self, tmp_path):
        with pytest.raises(LaneBookError, match="nope.yaml"):
            LaneBook.load(tmp_path / "nope.yaml")


class TestTheCommittedFile:
    """The file the operator actually runs against."""

    @pytest.fixture
    def committed(self):
        from pathlib import Path

        return LaneBook.load(Path(__file__).parent.parent / "config" / "lanes.yaml")

    def test_it_parses(self, committed):
        assert committed.bands and committed.states

    def test_the_default_band_is_the_hottest_one(self, committed):
        # The whole safety argument rests on this, so it is asserted rather
        # than left to a reader noticing.
        assert committed.bands[committed.default_band] == max(committed.bands.values())

    def test_every_mapped_state_is_a_two_letter_code(self, committed):
        assert all(len(code) == 2 and code.isalpha() for code in committed.states)

    def test_it_actually_widens_the_envelope_somewhere(self, committed):
        # The point of the change: a single national 22C left the gate topping
        # out at two elapsed days everywhere. Cool lanes have to come out
        # cooler than that or nothing was gained.
        coolest = min(
            committed.lane_for(state, AUGUST).ambient_c for state in committed.states
        )
        assert coolest < DEFAULT_LANE.ambient_c

    def test_and_makes_hot_lanes_honestly_worse(self, committed):
        # The other half, and the one that costs money: an August shipment to
        # Texas is not a 22C problem and should not be priced as one.
        assert committed.lane_for("TX", AUGUST).ambient_c > DEFAULT_LANE.ambient_c
