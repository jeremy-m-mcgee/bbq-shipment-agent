from datetime import date

import pytest

from bbq_shipment_agent.planning import (
    CARRIER_PAIRS,
    MAX_CARRIERS_PER_RUN,
    STATIC_RATES,
    BoxSize,
    Carrier,
    Lane,
    Shipment,
    StaticRateCard,
    billable_weight_kg,
    define_load,
    enumerate_configurations,
    shipment_options,
    solve_pairs,
)

SATURDAY = date(2026, 8, 8)
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
ALL_DAYS = (SATURDAY, MONDAY, TUESDAY)
WEEKDAYS = (MONDAY, TUESDAY)


def shipments(count: int, **overrides) -> tuple[Shipment, ...]:
    return tuple(
        Shipment(
            recipient_key=f"r{i:02d}",
            name=f"Recipient {i}",
            address={"street": f"{i} Main St", "zip": "78701"},
            **overrides,
        )
        for i in range(count)
    )


class TestRates:
    def test_the_large_box_is_billed_on_volume_it_is_not_using(self):
        # Design 5: at 1.5 lb the large box is nowhere near its dimensional
        # weight in actual mass, so it loses on cost as well as thermally.
        load = define_load("k")
        configurations = enumerate_configurations(load, (MONDAY,))
        by_shape = {
            (c.box_size, c.gel_packs): c
            for c in configurations
            if c.service.key == "usps:priority"
        }
        small = billable_weight_kg(load, by_shape[(BoxSize.SMALL, 6)])
        large = billable_weight_kg(load, by_shape[(BoxSize.LARGE, 6)])
        assert large > small

    def test_the_large_box_quotes_higher_for_the_same_contents(self):
        load = define_load("k")
        card = StaticRateCard()
        ship = Shipment(recipient_key="k", name="K")
        configurations = {
            (c.box_size): c
            for c in enumerate_configurations(load, (MONDAY,))
            if c.service.key == "usps:priority" and c.gel_packs == 6
        }
        assert card.quote(load, configurations[BoxSize.LARGE], ship) > card.quote(
            load, configurations[BoxSize.SMALL], ship
        )

    def test_faster_service_costs_more(self):
        # The ordering is what should survive replacement by real Shippo
        # rates, not any particular figure.
        assert STATIC_RATES["fedex:overnight"].base > STATIC_RATES["fedex:second_day"].base
        assert STATIC_RATES["fedex:second_day"].base > STATIC_RATES["fedex:ground"].base

    def test_a_further_zone_costs_more(self):
        load = define_load("k")
        card = StaticRateCard()
        configuration = next(
            c for c in enumerate_configurations(load, (MONDAY,)) if c.gel_packs == 6
        )
        near = card.quote(load, configuration, Shipment("k", "K", zone=1))
        far = card.quote(load, configuration, Shipment("k", "K", zone=8))
        assert far > near

    def test_an_unpriceable_service_is_dropped_not_raised(self):
        # A carrier that does not serve a zone is an ordinary planning fact.
        empty_card = StaticRateCard(rates={})
        assert shipment_options(Shipment("k", "K"), ALL_DAYS, empty_card) == ()


class TestPairEnumeration:
    def test_four_carriers_choose_two_is_six(self):
        assert len(CARRIER_PAIRS) == 6
        assert all(len(pair) == MAX_CARRIERS_PER_RUN for pair in CARRIER_PAIRS)

    def test_every_carrier_appears(self):
        assert {c for pair in CARRIER_PAIRS for c in pair} == set(Carrier)

    def test_pairs_are_ordered_deterministically(self):
        assert CARRIER_PAIRS == tuple(sorted(CARRIER_PAIRS))


class TestSolve:
    def test_it_scores_all_six_pairs(self):
        solve = solve_pairs(shipments(5), ALL_DAYS)
        assert len(solve.covering) + len(solve.partial) == 6

    def test_covering_pairs_rank_by_cost(self):
        solve = solve_pairs(shipments(5), ALL_DAYS)
        costs = [p.total_cost for p in solve.covering]
        assert costs == sorted(costs)

    def test_the_best_plan_is_the_cheapest_covering_one(self):
        solve = solve_pairs(shipments(5), ALL_DAYS)
        assert solve.best is solve.covering[0]
        assert solve.best.covers_all

    def test_each_shipment_gets_its_cheapest_option_under_the_pair(self):
        # Independent lookup: no interaction between shipments, which is why
        # design 1 says no solver is needed.
        ships = shipments(4)
        solve = solve_pairs(ships, ALL_DAYS)
        plan = solve.best
        allowed = set(plan.carriers)
        for assignment in plan.assignments:
            available = [
                p
                for p in shipment_options(assignment.shipment, ALL_DAYS)
                if p.carrier in allowed
            ]
            assert assignment.cost == min(p.cost for p in available)

    def test_total_cost_is_the_sum_of_assignments(self):
        plan = solve_pairs(shipments(6), ALL_DAYS).best
        assert plan.total_cost == pytest.approx(sum(a.cost for a in plan.assignments))

    def test_a_partial_plan_never_outranks_a_covering_one(self):
        # A cheap plan that quietly drops three recipients must not win.
        ships = shipments(3) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        solve = solve_pairs(ships, WEEKDAYS)
        assert solve.covering and solve.partial
        assert all(p.covers_all for p in solve.covering)
        assert solve.best.covers_all

    def test_partial_plans_rank_by_coverage_then_cost(self):
        ships = shipments(3) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        solve = solve_pairs(ships, WEEKDAYS)
        coverages = [p.coverage for p in solve.partial]
        assert coverages == sorted(coverages, reverse=True)

    def test_carriers_used_can_be_narrower_than_the_pair(self):
        # At most two is a ceiling, not a quota. The manifest should report
        # what is actually being dropped off.
        plan = solve_pairs(shipments(5), ALL_DAYS).best
        assert set(plan.carriers_used) <= set(plan.carriers)

    def test_packets_by_date_accounts_for_every_shipment(self):
        plan = solve_pairs(shipments(22), ALL_DAYS).best
        assert sum(plan.packets_by_date().values()) == plan.coverage == 22

    def test_minimum_thermal_margin_is_reported(self):
        plan = solve_pairs(shipments(5), ALL_DAYS).best
        assert plan.min_thermal_margin_c == min(
            a.priced.thermal_margin_c for a in plan.assignments
        )

    def test_the_solve_is_deterministic(self):
        ships = shipments(22)
        first = solve_pairs(ships, ALL_DAYS)
        second = solve_pairs(ships, ALL_DAYS)
        assert [p.carriers for p in first.covering] == [p.carriers for p in second.covering]
        assert [
            (a.shipment.recipient_key, a.priced.configuration)
            for a in first.best.assignments
        ] == [
            (a.shipment.recipient_key, a.priced.configuration)
            for a in second.best.assignments
        ]


class TestStrandedVersusInfeasible:
    """Design 4 draws this line explicitly, and conflating it turns a
    reportable tradeoff into a mysteriously missing recipient."""

    def test_feasible_elsewhere_but_not_here_is_stranded(self):
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        solve = solve_pairs(ships, WEEKDAYS)
        without_usps = [p for p in solve.partial if Carrier.USPS not in p.carriers]
        assert without_usps
        assert all("pinned" in p.stranded for p in without_usps)
        assert solve.infeasible == ()

    def test_feasible_nowhere_is_infeasible_not_stranded(self):
        # C3 came back empty for this shipment; that belongs to C4.
        ships = shipments(2) + (Shipment("hot", "Hot", lane=Lane("oven", 60.0)),)
        solve = solve_pairs(ships, ALL_DAYS)
        assert solve.infeasible == ("hot",)
        assert all("hot" not in p.stranded for p in solve.covering + solve.partial)

    def test_an_infeasible_shipment_does_not_block_coverage(self):
        # Otherwise one undeliverable destination would make every pair
        # partial and there would be no plan to review at all.
        ships = shipments(2) + (Shipment("hot", "Hot", lane=Lane("oven", 60.0)),)
        solve = solve_pairs(ships, ALL_DAYS)
        assert solve.covering
        assert solve.best.coverage == 2


class TestSaturdayForcing:
    """Design 3's "highest-leverage interaction in the planning stage"."""

    def test_a_pinned_saturday_shipment_is_saturday_only(self):
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        assert solve_pairs(ships, WEEKDAYS).saturday_only == ("pinned",)

    def test_it_forces_usps_into_every_covering_pair(self):
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        solve = solve_pairs(ships, WEEKDAYS)
        assert solve.covering
        assert all(Carrier.USPS in p.carriers for p in solve.covering)

    def test_and_the_forcing_is_stated_not_left_implicit(self):
        # Design 4, D2: the reason is stated rather than buried in the
        # ranking, because the operator needs to know why USPS is there.
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        solve = solve_pairs(ships, WEEKDAYS)
        assert all(p.forced_by_saturday for p in solve.covering)

    def test_pairs_without_usps_strand_it(self):
        # "leaves exactly one free slot for the remaining shipments"
        ships = shipments(2) + (
            Shipment("pinned", "Pinned", required_ship_date=SATURDAY),
        )
        solve = solve_pairs(ships, WEEKDAYS)
        assert {p.carriers for p in solve.partial} == {
            pair for pair in CARRIER_PAIRS if Carrier.USPS not in pair
        }

    def test_no_pinning_means_no_forcing(self):
        # Saturday offers a strict subset of weekday services and ship day
        # does not enter the thermal calculation, so nothing else can make a
        # shipment Saturday-only.
        solve = solve_pairs(shipments(5), ALL_DAYS)
        assert solve.saturday_only == ()
        assert not any(p.forced_by_saturday for p in solve.covering)
