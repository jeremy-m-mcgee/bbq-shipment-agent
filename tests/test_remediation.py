"""C4, deterministic. Design section 4, Phase C, and section 6.3."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from bbq_shipment_agent.planning import (
    Address,
    Lane,
    LaneBook,
    RecordedQuoter,
    RemediationMove,
    Shipment,
    remediate,
    remediate_all,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shippo-quotes-sf-dc.json"
ORIGIN = Address("BBQ HQ", "64 Divisadero St", "San Francisco", "CA", "94117")
DEST = Address("Ana Ruiz", "1600 Pennsylvania Ave NW", "Washington", "DC", "20500-0005")
DATES = (date(2026, 8, 17),)

BOOK = LaneBook.from_mapping(
    {
        "default_band": "hot",
        "bands": {"hot": 32.0, "cool": 12.0},
        "states": {"DC": "hot"},
        # August is the worst month on this book; a later one drops far
        # enough for the same quoted services to clear.
        "seasonal_offset_c": {8: 8, 10: 0, 1: -20},
    }
)


@pytest.fixture
def quoter():
    return RecordedQuoter.from_file(FIXTURE)


def shipment(ambient):
    return Shipment(
        recipient_key="ana",
        name="Ana Ruiz",
        address=DEST,
        lane=Lane(key="test", ambient_c=ambient),
    )


class TestDeferral:
    def test_a_cooler_month_rescues_a_shipment(self, quoter):
        result = remediate(shipment(70.0), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert result.move is RemediationMove.DEFER
        assert result.earliest_month is not None
        assert result.deferred_ambient_c < result.current_ambient_c

    def test_it_names_the_month_and_what_would_clear(self, quoter):
        result = remediate(shipment(70.0), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert "defer to" in result.describe()
        assert "gel" in result.reason  # the configuration that clears

    def test_it_says_how_far_short_the_current_best_fell(self, quoter):
        # "Two hours of hold time" and "a full day" call for different
        # answers, and a filtered list cannot tell them apart.
        result = remediate(shipment(70.0), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert result.best_margin_c is not None and result.best_margin_c < 0
        assert "over the 4.4C limit" in result.reason

    def test_it_warns_that_a_future_rate_is_not_this_quote(self, quoter):
        result = remediate(shipment(70.0), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert "feasibility answer, not a quote" in result.reason


class TestUndeliverable:
    def test_a_lane_no_month_can_save_is_undeliverable(self, quoter):
        book = LaneBook.from_mapping(
            {
                "default_band": "blistering",
                "bands": {"blistering": 95.0},
                "states": {"DC": "blistering"},
            }
        )
        result = remediate(shipment(95.0), ORIGIN, DATES, quoter, lane_book=book)
        assert result.move is RemediationMove.UNDELIVERABLE
        assert result.earliest_month is None
        assert "No month in the next 12" in result.reason

    def test_without_a_lane_book_it_says_that_is_why(self, quoter):
        # The honest failure: the search could not run, rather than the
        # destination being unservable.
        result = remediate(shipment(95.0), ORIGIN, DATES, quoter, lane_book=None)
        assert result.move is RemediationMove.UNDELIVERABLE
        assert "No lane configuration is loaded" in result.reason


class TestItOnlyProposes:
    def test_it_never_relaxes_the_gate(self, quoter):
        # C4 is invoked *because* the 4.4C gate refused everything, which makes
        # it the one place relaxing the gate would look like a solution. Every
        # deferral it offers has to clear the same threshold.
        from bbq_shipment_agent.planning import MAX_ARRIVAL_TEMP_C

        result = remediate(shipment(70.0), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert result.move is RemediationMove.DEFER
        # The quoted arrival in the reason is at or below the limit.
        arrival = float(result.reason.split("would arrive")[1].split("C")[0])
        assert arrival <= MAX_ARRIVAL_TEMP_C

    def test_it_returns_a_recommendation_not_a_change(self, quoter):
        original = shipment(70.0)
        remediate(original, ORIGIN, DATES, quoter, lane_book=BOOK)
        # Frozen dataclass, but the point is that nothing was rewritten:
        # accepting a deferral is D2's job and a human's decision.
        assert original.required_ship_date is None
        assert original.lane.ambient_c == 70.0


class TestAcrossARun:
    def test_only_the_infeasible_are_remediated(self, quoter):
        ships = (shipment(70.0), replace(shipment(70.0), recipient_key="bea"))
        results = remediate_all(ships, ("ana",), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert [r.recipient_key for r in results] == ["ana"]

    def test_a_run_with_nothing_infeasible_does_no_work(self, quoter):
        assert remediate_all((shipment(22.0),), (), ORIGIN, DATES, quoter) == ()

    def test_a_feasible_shipment_is_refused_rather_than_deferred(self, quoter):
        # C5's tradeoff, not C4's failure. Answering would recommend deferring
        # a shipment that ships fine today. 40C still clears on a one-day
        # service, which is exactly how this was found.
        with pytest.raises(ValueError, match="global infeasibility only"):
            remediate(shipment(40.0), ORIGIN, DATES, quoter, lane_book=BOOK)


class TestItIsNotAnAgent:
    def test_the_registry_has_three_agents(self):
        # Design 6.3: C4's only open-ended move was splitting, and splitting is
        # impossible. What remained is arithmetic, and design 2 keeps a model
        # out of the path of a decision that can be computed.
        from bbq_shipment_agent.agent_configs import AGENT_KEYS

        assert set(AGENT_KEYS) == {
            "address-repair",
            "manifest-verification",
            "review-narrator",
        }

    def test_remediating_writes_no_invocation_record(self, quoter, tmp_path):
        # Nothing to record, because nothing was invoked.
        from bbq_shipment_agent.ledger import AgentInvocationRecord, iter_records

        remediate_all((shipment(70.0),), ("ana",), ORIGIN, DATES, quoter, lane_book=BOOK)
        assert list(iter_records(tmp_path, AgentInvocationRecord)) == []
