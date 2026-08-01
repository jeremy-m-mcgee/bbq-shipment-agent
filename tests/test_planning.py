from datetime import date

import pytest

from bbq_shipment_agent.planning import (
    BOXES,
    DEFAULT_LANE,
    MAX_ARRIVAL_TEMP_C,
    MAX_CARRIERS_PER_RUN,
    BoxSize,
    Carrier,
    Lane,
    ShipDay,
    ShipDayError,
    define_load,
    enumerate_configurations,
    evaluate_configurations,
    services_for,
    ship_day_for,
    thermal_gate,
)

SATURDAY = date(2026, 8, 8)
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
WEDNESDAY = date(2026, 8, 12)
ALL_DAYS = (SATURDAY, MONDAY, TUESDAY)


@pytest.fixture
def load():
    return define_load("k1")


class TestShipDays:
    def test_it_classifies_the_three_shippable_days(self):
        assert ship_day_for(SATURDAY) is ShipDay.SATURDAY
        assert ship_day_for(MONDAY) is ShipDay.MONDAY
        assert ship_day_for(TUESDAY) is ShipDay.TUESDAY

    def test_a_non_shipping_day_is_refused_not_skipped(self):
        # A silently smaller enumeration is a much worse way to discover the
        # mistake than an exception naming the date.
        with pytest.raises(ShipDayError, match="Wednesday"):
            ship_day_for(WEDNESDAY)

    def test_saturday_is_usps_only(self):
        # Design 3: USPS only for perishables on Saturday, given weekend
        # ground schedules.
        assert {s.carrier for s in services_for(ShipDay.SATURDAY)} == {Carrier.USPS}

    def test_weekdays_offer_every_carrier(self):
        assert {s.carrier for s in services_for(ShipDay.MONDAY)} == set(Carrier)


class TestBoxGeometry:
    def test_the_larger_box_leaks_faster(self):
        # Design 5's central claim: at 1.5 lb more surface area is strictly
        # worse, with no compensating ballast because the mass is not there.
        assert BOXES[BoxSize.LARGE].ua_w_k > BOXES[BoxSize.SMALL].ua_w_k

    def test_ua_is_computed_from_geometry_not_stored(self, load):
        # Step 4 computes UA from geometry and material properties, so the box
        # must expose the geometry rather than a precomputed answer.
        box = BOXES[BoxSize.SMALL]
        expected = box.conductivity_w_mk * box.surface_area_m2 / box.wall_m
        assert box.ua_w_k == pytest.approx(expected)

    def test_the_packet_fits_both_boxes(self, load):
        assert all(load.fits_in(box) for box in BOXES.values())


class TestEnumeration:
    def test_it_covers_every_ship_day(self, load):
        configurations = enumerate_configurations(load, ALL_DAYS)
        assert {c.ship_date for c in configurations} == set(ALL_DAYS)

    def test_no_saturday_configuration_uses_another_carrier(self, load):
        configurations = enumerate_configurations(load, ALL_DAYS)
        saturday = [c for c in configurations if c.ship_date == SATURDAY]
        assert saturday
        assert {c.carrier for c in saturday} == {Carrier.USPS}

    def test_all_four_carriers_survive_enumeration(self, load):
        # Design 4, C2: all four carriers at this stage, no pair restriction.
        # The pair limit is C5's job and applying it here would turn a
        # tradeoff into an infeasibility.
        configurations = enumerate_configurations(load, ALL_DAYS)
        assert {c.carrier for c in configurations} == set(Carrier)

    def test_zero_gel_packs_is_enumerated(self, load):
        # It will not survive C3, but the gate should be the only thing that
        # decides feasibility.
        configurations = enumerate_configurations(load, (MONDAY,))
        assert any(c.gel_packs == 0 for c in configurations)

    def test_gel_packs_are_bounded_by_the_box(self, load):
        for configuration in enumerate_configurations(load, ALL_DAYS):
            assert 0 <= configuration.gel_packs <= configuration.box.max_gel_packs

    def test_a_non_shipping_date_is_refused(self, load):
        with pytest.raises(ShipDayError):
            enumerate_configurations(load, (MONDAY, WEDNESDAY))

    def test_no_dates_is_an_error(self, load):
        with pytest.raises(ValueError, match="no candidate ship dates"):
            enumerate_configurations(load, ())

    def test_the_space_stays_small_enough_to_brute_force(self, load):
        # Design 1: small enough to enumerate exhaustively, no solver needed.
        assert len(enumerate_configurations(load, ALL_DAYS)) < 1000


class TestThermalGate:
    def test_the_threshold_is_the_documented_constant(self):
        assert MAX_ARRIVAL_TEMP_C == 4.4

    def test_zero_gel_packs_never_survives(self, load):
        configurations = enumerate_configurations(load, ALL_DAYS)
        feasible = thermal_gate(load, configurations)
        assert feasible
        assert all(f.configuration.gel_packs > 0 for f in feasible)

    def test_more_gel_packs_never_arrives_warmer(self, load):
        # Monotonicity is the property that should survive recalibration,
        # unlike any particular constant in the placeholder model.
        configurations = enumerate_configurations(load, (MONDAY,))
        by_shape = {
            (e.configuration.box_size, e.configuration.gel_packs, e.configuration.service.key): e
            for e in evaluate_configurations(load, configurations)
        }
        service = "ups:next_day"
        temps = [
            by_shape[(BoxSize.SMALL, n, service)].predicted_arrival_temp_c
            for n in range(0, 7)
        ]
        assert temps == sorted(temps, reverse=True)

    def test_the_larger_box_is_never_thermally_better(self, load):
        configurations = enumerate_configurations(load, (MONDAY,))
        evaluated = {
            (e.configuration.box_size, e.configuration.gel_packs, e.configuration.service.key): e
            for e in evaluate_configurations(load, configurations)
        }
        for gel in range(0, 7):
            small = evaluated[(BoxSize.SMALL, gel, "ups:next_day")]
            large = evaluated[(BoxSize.LARGE, gel, "ups:next_day")]
            assert (
                large.predicted_arrival_temp_c >= small.predicted_arrival_temp_c
            ), f"large box beat small at {gel} gel packs"

    def test_longer_transit_never_arrives_colder(self, load):
        configurations = enumerate_configurations(load, (MONDAY,))
        evaluated = [
            e
            for e in evaluate_configurations(load, configurations)
            if e.configuration.box_size is BoxSize.SMALL
            and e.configuration.gel_packs == 6
            and e.configuration.carrier is Carrier.FEDEX
        ]
        by_days = {e.configuration.transit_days: e for e in evaluated}
        assert (
            by_days[1].predicted_arrival_temp_c
            <= by_days[2].predicted_arrival_temp_c
            <= by_days[3].predicted_arrival_temp_c
        )

    def test_a_warmer_lane_is_never_easier(self, load):
        configurations = enumerate_configurations(load, (MONDAY,))
        cool = thermal_gate(load, configurations, Lane("cool", ambient_c=10.0))
        warm = thermal_gate(load, configurations, Lane("warm", ambient_c=35.0))
        assert len(cool) >= len(warm)

    def test_margin_is_headroom_below_the_threshold(self, load):
        configurations = enumerate_configurations(load, (MONDAY,))
        for evaluated in evaluate_configurations(load, configurations):
            assert evaluated.thermal_margin_c == pytest.approx(
                MAX_ARRIVAL_TEMP_C - evaluated.predicted_arrival_temp_c
            )
            assert evaluated.feasible == (evaluated.thermal_margin_c >= 0)

    def test_an_impossible_lane_gates_everything_out(self, load):
        # C3 is expected to come back empty sometimes; design 4 routes that to
        # C4 rather than treating it as an error.
        configurations = enumerate_configurations(load, ALL_DAYS)
        assert thermal_gate(load, configurations, Lane("oven", ambient_c=60.0)) == ()

    def test_evaluation_keeps_the_near_misses_the_gate_drops(self, load):
        # C4 needs to know how far short the best option fell; a filtered list
        # cannot tell "two hours" from "a full day".
        configurations = enumerate_configurations(load, ALL_DAYS)
        assert len(evaluate_configurations(load, configurations)) == len(configurations)
        assert len(thermal_gate(load, configurations)) < len(configurations)

    def test_the_lane_is_recorded_on_every_evaluation(self, load):
        # A thermal record whose ambient assumption is unlabelled is exactly
        # what design 5 asks to avoid.
        lane = Lane("gulf-summer", ambient_c=31.0, zone=6)
        configurations = enumerate_configurations(load, (MONDAY,))
        assert all(e.lane is lane for e in evaluate_configurations(load, configurations, lane))

    def test_a_custom_model_can_be_injected(self, load):
        # The swap point design 5 promises: step 4 replaces the model without
        # touching the gate or the threshold.
        class AlwaysFreezing:
            def predict_arrival_temp_c(self, load, configuration, lane):
                return -5.0

        configurations = enumerate_configurations(load, ALL_DAYS)
        feasible = thermal_gate(load, configurations, DEFAULT_LANE, AlwaysFreezing())
        assert len(feasible) == len(configurations)

    def test_a_model_cannot_widen_the_threshold(self, load):
        # The gate owns the threshold, so a recalibrated model can never
        # quietly change what counts as safe.
        class JustOverTheLine:
            def predict_arrival_temp_c(self, load, configuration, lane):
                return MAX_ARRIVAL_TEMP_C + 0.01

        configurations = enumerate_configurations(load, ALL_DAYS)
        assert thermal_gate(load, configurations, DEFAULT_LANE, JustOverTheLine()) == ()


class TestPlaceholderArtefact:
    """The cliff documented in `thermal`'s module docstring, pinned.

    These do not describe a requirement — they pin a known property of the
    stand-in so step 4 has to consciously break them rather than silently
    inheriting a degenerate margin distribution.
    """

    def test_feasible_margins_are_operationally_indistinguishable(self, load):
        # Not bit-identical: the 1-day cases sit a few microkelvin below the
        # gel temperature because the exponential has not quite converged.
        # Asserting the spread rather than equality states the property that
        # actually matters — nothing here can rank one margin above another.
        configurations = enumerate_configurations(load, ALL_DAYS)
        margins = [f.thermal_margin_c for f in thermal_gate(load, configurations)]
        assert max(margins) - min(margins) < 1e-3

    def test_which_leaves_the_run_minimum_margin_pinned_at_the_ceiling(self, load):
        # C5 ranks partly on minimum thermal margin across the run. Under this
        # model that field carries no information between carrier pairs.
        configurations = enumerate_configurations(load, ALL_DAYS)
        feasible = thermal_gate(load, configurations)
        assert min(f.thermal_margin_c for f in feasible) == pytest.approx(
            MAX_ARRIVAL_TEMP_C, abs=1e-3
        )


class TestRunConstraints:
    def test_the_carrier_limit_is_two(self):
        assert MAX_CARRIERS_PER_RUN == 2

    def test_every_carrier_is_reachable_after_gating(self, load):
        # If the placeholder gated a carrier out entirely, C5's six-pair solve
        # would be exercising far less than it looks.
        configurations = enumerate_configurations(load, ALL_DAYS)
        feasible = thermal_gate(load, configurations)
        assert {f.configuration.carrier for f in feasible} == set(Carrier)
