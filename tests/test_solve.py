"""Quoting and C5, against real recorded Shippo quotes."""

from datetime import date
from pathlib import Path

import pytest

from bbq_shipment_agent.planning import (
    MAX_CARRIERS_PER_RUN,
    Address,
    CarrierMessage,
    Lane,
    ParcelSpec,
    Quote,
    QuoteResult,
    QuotingUnavailable,
    RecordedQuoter,
    Shipment,
    ShippoQuoter,
    carrier_subsets,
    define_load,
    heaviest_variant,
    parcel_variants,
    pin_carriers,
    shipment_options,
    solve_carriers,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shippo-quotes-sf-dc.json"
ORIGIN = Address("BBQ Kitchen", "64 Divisadero St", "San Francisco", "CA", "94117")
DEST = Address("Recipient One", "1600 Pennsylvania Ave NW", "Washington", "DC", "20500")

SATURDAY = date(2026, 8, 8)
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
ALL_DAYS = (SATURDAY, MONDAY, TUESDAY)
WEEKDAYS = (MONDAY, TUESDAY)


@pytest.fixture
def quoter():
    return RecordedQuoter.from_file(FIXTURE)


def shipments(count: int, **overrides) -> tuple[Shipment, ...]:
    return tuple(
        Shipment(recipient_key=f"r{i:02d}", name=f"Recipient {i}", address=DEST, **overrides)
        for i in range(count)
    )


class TestCarrierMessages:
    def test_rate_limiting_is_recognised_as_transient(self):
        assert CarrierMessage("UPS", "10429", "Hard: Too Many Requests").transient

    def test_a_service_area_rejection_is_not(self):
        # Retrying a permanent rejection burns time and reports the same thing.
        assert not CarrierMessage(
            "UPS", "", "Shipment origin is out of service area for UPS Master account"
        ).transient

    def test_an_unrecognised_message_is_treated_as_a_real_answer(self):
        assert not CarrierMessage("DHLExpress", "", "does not support US domestic").transient


class TestQuotePinning:
    def test_the_pin_is_what_actually_quoted(self, quoter):
        load = define_load("r0")
        reference = heaviest_variant(parcel_variants(load))
        assert pin_carriers(quoter, ORIGIN, DEST, reference) == frozenset({"UPS", "USPS"})

    def test_a_recording_missing_a_pinned_carrier_raises(self, quoter):
        # The pin exists because quoting the same lane with different parcels
        # returned UPS for some and not others. Planning against a short
        # carrier set reports strandings that are artefacts of the API call.
        load = define_load("r0")
        parcel = parcel_variants(load)[0]
        with pytest.raises(QuotingUnavailable, match="missing pinned carriers"):
            quoter.quote(ORIGIN, DEST, parcel, require=frozenset({"UPS", "FedEx"}))

    def test_missing_reports_the_gap(self):
        result = QuoteResult(quotes=(), messages=())
        assert result.missing(frozenset({"UPS"})) == frozenset({"UPS"})


class TestQuoterRetry:
    """Transient failures become latency rather than a short carrier set."""

    def _quoter(self, responses):
        class FakeShippo(ShippoQuoter):
            def __init__(self, responses):
                super().__init__(api_key="test", sleep=lambda _s: None, max_attempts=4)
                self._responses = list(responses)
                self.calls = 0

            def _fetch(self, origin, destination, parcel, attempt):
                self.calls += 1
                return self._responses.pop(0)

        return FakeShippo(responses)

    def _quote(self, carrier="UPS", amount=10.0):
        parcel = ParcelSpec(  # shape only; never sent anywhere in this test
            box_size=next(iter(parcel_variants(define_load("r0")))).box_size,
            gel_packs=0, length_cm=25, width_cm=25, height_cm=25, weight_kg=1.0,
        )
        return Quote(carrier, "svc", "Service", amount, "USD", 2, parcel)

    def test_it_retries_until_the_pinned_carrier_answers(self):
        throttled = QuoteResult(
            quotes=(self._quote("USPS"),),
            messages=(CarrierMessage("UPS", "10429", "Hard: Too Many Requests"),),
        )
        complete = QuoteResult(quotes=(self._quote("USPS"), self._quote("UPS")))
        quoter = self._quoter([throttled, throttled, complete])
        parcel = parcel_variants(define_load("r0"))[0]

        result = quoter.quote(ORIGIN, DEST, parcel, require=frozenset({"UPS", "USPS"}))
        assert result.carriers == {"UPS", "USPS"}
        assert quoter.calls == 3

    def test_attempts_counts_calls_made_not_which_one_won(self):
        # The failure message said "after 1 attempts" when four had been made,
        # which reads as a retry that never ran.
        throttled = QuoteResult(
            quotes=(self._quote("USPS"),),
            messages=(CarrierMessage("UPS", "10429", "Too Many Requests"),),
        )
        quoter = self._quoter([throttled] * 4)
        parcel = parcel_variants(define_load("r0"))[0]
        with pytest.raises(QuotingUnavailable, match="after 4 attempts"):
            quoter.quote(ORIGIN, DEST, parcel, require=frozenset({"UPS", "USPS"}))

    def test_it_does_not_retry_a_permanent_rejection(self):
        settled = QuoteResult(
            quotes=(self._quote("USPS"),),
            messages=(CarrierMessage("DHLExpress", "", "no US domestic"),),
        )
        quoter = self._quoter([settled])
        parcel = parcel_variants(define_load("r0"))[0]
        assert quoter.quote(ORIGIN, DEST, parcel).carriers == {"USPS"}
        assert quoter.calls == 1

    def test_no_quotes_at_all_stops_the_run(self):
        # Unlike the LaunchDarkly path, quoting is not optional: a missing
        # rate costs a manifest full of invented money.
        quoter = self._quoter([QuoteResult(quotes=())])
        parcel = parcel_variants(define_load("r0"))[0]
        with pytest.raises(QuotingUnavailable, match="no carrier quoted"):
            quoter.quote(ORIGIN, DEST, parcel)


class TestCarrierSubsets:
    def test_it_enumerates_singletons_and_pairs(self):
        assert carrier_subsets(frozenset({"UPS", "USPS"})) == (
            ("UPS",), ("USPS",), ("UPS", "USPS"),
        )

    def test_four_carriers_give_ten_subsets_not_six_pairs(self):
        # "At most two" is a ceiling, not a quota, and a single carrier
        # serving everything is a legal and often winning answer.
        subsets = carrier_subsets(frozenset({"A", "B", "C", "D"}))
        assert len(subsets) == 10
        assert all(len(s) <= MAX_CARRIERS_PER_RUN for s in subsets)

    def test_a_single_available_carrier_still_yields_a_plan(self):
        assert carrier_subsets(frozenset({"USPS"})) == (("USPS",),)

    def test_ordering_is_deterministic(self):
        assert carrier_subsets(frozenset({"C", "A", "B"})) == carrier_subsets(
            frozenset({"B", "C", "A"})
        )


class TestSolve:
    def test_it_scores_every_legal_subset(self, quoter):
        solve = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter)
        assert len(solve.covering) + len(solve.partial) == len(
            carrier_subsets(solve.available_carriers)
        )

    def test_covering_subsets_rank_by_cost(self, quoter):
        solve = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter)
        costs = [p.total_cost for p in solve.covering]
        assert costs == sorted(costs)

    def test_the_best_plan_is_the_cheapest_covering_one(self, quoter):
        solve = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter)
        assert solve.best is solve.covering[0]
        assert solve.best.covers_all

    def test_a_cheaper_single_carrier_beats_a_pair_that_ties(self, quoter):
        # Equal cost resolves toward fewer carriers, which matches design 3's
        # rationale for the ceiling: operational simplicity at drop-off.
        solve = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter)
        assert len(solve.best.carriers) == 1

    def test_each_shipment_gets_its_cheapest_option_under_the_subset(self, quoter):
        ships = shipments(2)
        solve = solve_carriers(ships, ORIGIN, ALL_DAYS, quoter)
        allowed = set(solve.best.carriers)
        feasible, _ = shipment_options(ships[0], ORIGIN, ALL_DAYS, quoter)
        cheapest = min(
            e.configuration.cost for e in feasible if e.configuration.carrier in allowed
        )
        assert solve.best.assignments[0].cost == cheapest

    def test_total_cost_is_the_sum_of_assignments(self, quoter):
        plan = solve_carriers(shipments(3), ORIGIN, ALL_DAYS, quoter).best
        assert plan.total_cost == pytest.approx(sum(a.cost for a in plan.assignments))

    def test_packets_by_date_accounts_for_every_shipment(self, quoter):
        plan = solve_carriers(shipments(4), ORIGIN, ALL_DAYS, quoter).best
        assert sum(plan.packets_by_date().values()) == plan.coverage == 4

    def test_carriers_used_can_be_narrower_than_the_subset(self, quoter):
        plan = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter).best
        assert set(plan.carriers_used) <= set(plan.carriers)

    def test_minimum_thermal_margin_is_reported(self, quoter):
        plan = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter).best
        assert plan.min_thermal_margin_c == min(
            a.evaluated.thermal_margin_c for a in plan.assignments
        )

    def test_the_cheapest_plan_can_sit_on_a_thin_margin(self, quoter):
        # Real behaviour worth pinning: cost-first ranking will happily pick a
        # configuration close to the gate, which is what D1 exists to notice.
        plan = solve_carriers(shipments(2), ORIGIN, ALL_DAYS, quoter).best
        assert plan.min_thermal_margin_c < 1.0

    def test_the_solve_is_deterministic(self, quoter):
        ships = shipments(4)
        first = solve_carriers(ships, ORIGIN, ALL_DAYS, quoter)
        second = solve_carriers(ships, ORIGIN, ALL_DAYS, quoter)
        assert [p.carriers for p in first.covering] == [p.carriers for p in second.covering]
        assert [a.evaluated.configuration.describe() for a in first.best.assignments] == [
            a.evaluated.configuration.describe() for a in second.best.assignments
        ]

    def test_carrier_messages_reach_the_result(self, quoter):
        assert isinstance(solve_carriers(shipments(1), ORIGIN, ALL_DAYS, quoter).messages, tuple)


class TestStrandedVersusInfeasible:
    """Design 4 draws this line explicitly; conflating it turns a reportable
    tradeoff into a mysteriously missing recipient."""

    def test_feasible_nowhere_is_infeasible_not_stranded(self, quoter):
        ships = shipments(1) + (
            Shipment("hot", "Hot", address=DEST, lane=Lane("furnace", 80.0)),
        )
        solve = solve_carriers(ships, ORIGIN, ALL_DAYS, quoter)
        assert solve.infeasible == ("hot",)
        assert all("hot" not in p.stranded for p in solve.covering + solve.partial)

    def test_an_infeasible_shipment_does_not_block_coverage(self, quoter):
        # Otherwise one undeliverable destination makes every subset partial
        # and there is no plan to review at all.
        ships = shipments(1) + (
            Shipment("hot", "Hot", address=DEST, lane=Lane("furnace", 80.0)),
        )
        solve = solve_carriers(ships, ORIGIN, ALL_DAYS, quoter)
        assert solve.covering and solve.best.coverage == 1


class TestSaturdayForcing:
    """Design 3's "highest-leverage interaction in the planning stage"."""

    def _ships(self):
        return shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )

    def test_a_pinned_saturday_shipment_is_saturday_only(self, quoter):
        assert solve_carriers(self._ships(), ORIGIN, WEEKDAYS, quoter).saturday_only == (
            "pinned",
        )

    def test_it_forces_usps_into_every_covering_subset(self, quoter):
        solve = solve_carriers(self._ships(), ORIGIN, WEEKDAYS, quoter)
        assert solve.covering
        assert all("USPS" in p.carriers for p in solve.covering)

    def test_and_the_forcing_is_stated_not_left_implicit(self, quoter):
        # Design 4, D2: the operator needs to know why USPS is there.
        solve = solve_carriers(self._ships(), ORIGIN, WEEKDAYS, quoter)
        assert all(p.forced_by_saturday for p in solve.covering)

    def test_subsets_without_usps_strand_it(self, quoter):
        solve = solve_carriers(self._ships(), ORIGIN, WEEKDAYS, quoter)
        assert solve.partial
        assert all("pinned" in p.stranded for p in solve.partial)
        assert all("USPS" not in p.carriers for p in solve.partial)

    def test_no_pinning_means_no_forcing(self, quoter):
        # Saturday offers a strict subset of weekday carriers and ship day
        # does not enter the thermal calculation, so nothing else can make a
        # shipment Saturday-only.
        solve = solve_carriers(shipments(3), ORIGIN, ALL_DAYS, quoter)
        assert solve.saturday_only == ()
        assert not any(p.forced_by_saturday for p in solve.covering)


class TestTiesAndDuplicatePlans:
    """Both found by a live review that pinned a shipment onto Saturday."""

    def _saturday_forced(self, quoter):
        # One Saturday pin makes USPS mandatory, so the one-carrier subset and
        # the two-carrier one produce the same assignments at the same cost.
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", address=DEST, required_ship_date=SATURDAY),
        )
        return solve_carriers(ships, ORIGIN, (SATURDAY, MONDAY), quoter)

    def test_a_cost_tie_prefers_fewer_carriers(self, quoter):
        # Design 3 caps carriers at two for "operational simplicity at
        # drop-off"; the same reasoning prefers one over two at equal cost.
        # Tuple ordering used to pick ("UPS", "USPS") over ("USPS",).
        solve = self._saturday_forced(quoter)
        best = solve.best
        assert best is not None
        ties = [p for p in solve.covering if p.total_cost == best.total_cost]
        assert len(best.carriers) == min(len(p.carriers) for p in ties)

    def test_the_winner_uses_every_carrier_it_names(self, quoter):
        # A plan reported as two-carrier that makes one drop-off is a lie the
        # packer acts on.
        best = self._saturday_forced(quoter).best
        assert set(best.carriers_used) == set(best.carriers)

    def test_a_runner_up_identical_to_the_winner_is_dropped(self, quoter):
        solve = self._saturday_forced(quoter)
        best = solve.best
        for other in solve.runners_up:
            assert (other.carriers_used, other.total_cost) != (
                best.carriers_used,
                best.total_cost,
            ), "a runner-up at +$0.00 with the same carriers shows no tradeoff"

    def test_genuinely_different_runners_up_survive(self, quoter):
        # The filter must not empty the list it exists to populate.
        solve = solve_carriers(shipments(3), ORIGIN, (MONDAY,), quoter)
        if len(solve.covering) > 1:
            assert solve.runners_up
